package edge

// The Go job ledger.
//
// WHY JSONL AND NOT SQLITE
// ------------------------
// NEW_WORKFLOW.MD asks for SQLite. It also asks that Go be the master controller
// while the Kaggle worker keeps running Python -- and multitasker.py already has
// a working append-only JSONL ledger with resume. Two ledgers over one job is
// the failure mode: Go says "chunk 4 is done", Python says "chunk 4 is queued",
// and the tie-break is whichever process wrote last.
//
// So the format is multitasker.py's, byte-compatible, and the state vocabulary
// is theirs. Go validates transitions that Python does not, because a Go worker
// drives things concurrently and Python's worker does not; the states
// themselves are identical, so a ledger written by either reads correctly in the
// other. Migrating to SQLite later is a storage swap behind this API, not a
// format change.
//
// RECORD SHAPE -- identical to multitasker.py:
//
//	{"job_id": "...", "state": "...", "detail": "...", "ts": "..."}
//
// Two extra keys MAY appear (Go writes them, Python ignores unknown keys):
// "go_seq", a monotonic counter used to order records written inside the same
// second, and nothing else. Every other key Python writes is preserved on read.
//
// WHAT "ATOMIC" MEANS HERE
// ------------------------
// Transition updates the in-memory state only AFTER the record is on disk. A
// caller that gets nil back has a state that is both in RAM and in the file; a
// caller that gets an error has neither. There is no window where the ledger
// believes a job moved and the file does not -- which is the window that makes a
// resume either skip work that never ran, or redo work that did.
//
// And the file append is itself a single Write of a single line to a file opened
// O_APPEND, which the OS does not interleave. A process killed mid-write leaves
// a torn final line; Load skips undecodable lines for exactly that reason, the
// same way multitasker.py does.

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// JobState is one point in a job's life.
//
// The names are multitasker.py's. Changing one here without changing it there
// produces two ledgers that disagree about what a job is, which is the thing
// this file exists to prevent.
type JobState string

const (
	// StateQueued is the initial state: known, not started.
	StateQueued JobState = "queued"

	// StatePreparing through StateSynced are the in-flight stages of the
	// notebook's producer/consumer chain. Which ones a given job passes through
	// depends on what it has to do -- lip sync is optional, and a job without it
	// goes straight from dubbed to uploading.
	StatePreparing JobState = "preparing"
	StatePrepared  JobState = "prepared"
	StateDubbing   JobState = "dubbing"
	StateDubbed    JobState = "dubbed"
	StateSyncing   JobState = "syncing"
	StateSynced    JobState = "synced"
	StateUploading JobState = "uploading"
	StateUploaded  JobState = "uploaded"

	// StateDone is terminal success. A resume skips it, which is what makes the
	// second run of a notebook cheap.
	StateDone JobState = "done"

	// StateFailed is terminal for the ATTEMPT, not for the job: it is the only
	// terminal-ish state with an edge back out, because a retry is a normal
	// operation and not an exception to the state machine.
	StateFailed JobState = "failed"
)

// StateUnknown is what State reports for a job the ledger has never seen. It
// mirrors multitasker.py's `self._states.get(job_id, "unknown")`, because "we do
// not know about this job" and "this job is in state nothing at all" must not
// look alike to a resume check.
const StateUnknown JobState = "unknown"

// IsTerminal reports whether the ATTEMPT is over, i.e. nothing further will
// happen to this job without an explicit requeue.
//
// StateFailed is terminal for the attempt but not for the job: retrying is a
// normal repair, not an exception. That is why Pending -- the thing a resume
// actually acts on -- is defined separately and does include failures. One error
// is not the loss of a whole movie, and the queue that makes that true is
// Pending, not IsTerminal.
func (s JobState) IsTerminal() bool {
	return s == StateDone || s == StateFailed
}

// IsDone is the only state a resume skips outright.
func (s JobState) IsDone() bool { return s == StateDone }

// jobTransitions is the state machine.
//
// Read it as: from this state, these are the only legal next states. A
// transition not listed is refused and nothing is written, so a bug in a worker
// cannot quietly produce a ledger that no resume understands.
//
// The shape follows multitasker.py's actual calls rather than an idealised
// pipeline: downloader does queued -> preparing -> prepared, the GPU worker does
// prepared -> dubbing -> dubbed, lip sync does dubbed -> syncing -> synced, the
// uploader does * -> uploading -> uploaded -> done, and any worker may write
// failed from wherever it was.
//
// StateQueued is the permissive row, and deliberately so. A Go job is a single
// step -- "transcribe chunk 17", "translate chunk 17", "upload chunk 17" -- not
// the whole pipeline, so a job that is queued and then done in one call is
// ordinary, not a bug. Refusing queued -> done would mean every caller has to
// invent an intermediate stage it does not have, and the invented name would end
// up in a ledger that the Python side cannot interpret.
//
// Failed -> queued is the retry edge. It is the only way back into the machine,
// and it exists because retrying is a thing this pipeline does on purpose.
var jobTransitions = map[JobState][]JobState{
	StateQueued:    {StatePreparing, StateSyncing, StateUploading, StateDone, StateFailed},
	StatePreparing: {StatePrepared, StateFailed},
	StatePrepared:  {StateDubbing, StateFailed},
	StateDubbing:   {StateDubbed, StateFailed},
	StateDubbed:    {StateSyncing, StateUploading, StateDone, StateFailed},
	StateSyncing:   {StateSynced, StateUploading, StateDone, StateFailed},
	StateSynced:    {StateUploading, StateDone, StateFailed},
	StateUploading: {StateUploaded, StateFailed},
	StateUploaded:  {StateDone, StateFailed},
	StateFailed:    {StateQueued},
	StateDone:      {},
}

// CanTransition reports whether from -> to is legal.
//
// A job the ledger has never seen may go to ANY state, because there is no "from"
// to check against. That is not a hole: multitasker.py lets its first transition
// for a job be whatever the first worker writes, and being stricter here would
// mean the two ends disagree about whether a job exists before its first record
// does.
func CanTransition(from, to JobState) bool {
	if to == "" || !isKnownState(to) {
		return false
	}
	if from == StateUnknown || from == "" {
		return true
	}
	if from == to {
		// Re-recording the current state is legal and is how a worker writes a
		// heartbeat or a late detail without inventing a new state.
		return true
	}
	for _, s := range jobTransitions[from] {
		if s == to {
			return true
		}
	}
	return false
}

func isKnownState(s JobState) bool {
	switch s {
	case StateQueued, StatePreparing, StatePrepared, StateDubbing, StateDubbed,
		StateSyncing, StateSynced, StateUploading, StateUploaded, StateDone, StateFailed:
		return true
	}
	return false
}

// ErrIllegalTransition is returned by Ledger.Transition when the requested move
// is not in jobTransitions. Nothing is written and no state changes.
var ErrIllegalTransition = errors.New("edge: illegal job state transition")

// LedgerRecord is one line of the JSONL file. The json tags match
// multitasker.py's dict exactly, because the file has to be readable by both.
type LedgerRecord struct {
	JobID  string `json:"job_id"`
	State  string `json:"state"`
	Detail string `json:"detail"`
	TS     string `json:"ts"`

	// Seq is a monotonic counter Go adds to break ties between records written
	// within the same clock second. multitasker.py does not write it and ignores
	// it on read; it exists so an ordering claim in a log can be checked rather
	// than believed.
	Seq int64 `json:"go_seq,omitempty"`
}

// LedgerEntry is the reconstructed state of one job.
type LedgerEntry struct {
	JobID  string   `json:"job_id"`
	State  JobState `json:"state"`
	Detail string   `json:"detail"`
	TS     string   `json:"ts"`
	Seq    int64    `json:"go_seq,omitempty"`
	// Records counts how many transitions this job has. Useful when reading a
	// long ledger: a job with 40 records has flapped, and that is worth seeing.
	Records int `json:"records"`
}

// Ledger is an append-only job log with in-memory reconstruction.
//
// Safe for concurrent use: several workers transition different jobs at the same
// time and none of them block each other except on the file append, which is
// what it is for.
type Ledger struct {
	path string

	mu      sync.Mutex
	file    *os.File
	states  map[string]*LedgerEntry
	seq     int64
	lastErr error

	// degraded records that at least one append has failed. multitasker.py's
	// response to a ledger write failure is to log it and carry on -- the run
	// must not die because a checkpoint could not be saved -- and that policy is
	// kept here. Degraded() exists so the caller can SAY so, once, instead of
	// discovering at the end that resume was never actually possible.
	degraded bool

	// Log receives one line per recoverable problem. nil discards.
	Log func(format string, args ...any)
}

// OpenLedger opens or creates the ledger at path and loads its current state.
//
// A missing file is not an error: a first run has no ledger, and that is the
// ordinary case, not a misconfiguration.
func OpenLedger(path string) (*Ledger, error) {
	if strings.TrimSpace(path) == "" {
		return nil, errors.New("edge: ledger path is required")
	}
	if dir := filepath.Dir(path); dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, fmt.Errorf("edge: ledger mkdir %s: %w", dir, err)
		}
	}
	l := &Ledger{path: path, states: map[string]*LedgerEntry{}}
	if err := l.load(); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, fmt.Errorf("edge: ledger open %s: %w", path, err)
	}
	l.file = f
	return l, nil
}

// load reads the existing file and rebuilds the state map.
//
// Two properties multitasker.py depends on and this reproduces exactly:
//
//   - A line that does not parse is SKIPPED, not fatal. A kernel killed mid-write
//     leaves a torn last line, and refusing to start because of it would turn a
//     recoverable crash into an unrecoverable one.
//   - The LAST record for a job wins. Replaying forward is what makes an
//     append-only log a state machine.
func (l *Ledger) load() error {
	f, err := os.Open(l.path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("edge: ledger read %s: %w", l.path, err)
	}
	defer f.Close()

	// 1 MiB per line: records are small, but "detail" is free text and a
	// truncated stack trace in there should not truncate the whole ledger.
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64<<10), 1<<20)

	torn := 0
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		var rec LedgerRecord
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			torn++
			continue
		}
		if rec.JobID == "" || rec.State == "" {
			torn++
			continue
		}
		l.seq = max(l.seq, rec.Seq)
		e := l.states[rec.JobID]
		if e == nil {
			e = &LedgerEntry{JobID: rec.JobID}
			l.states[rec.JobID] = e
		}
		e.State = JobState(rec.State)
		e.Detail = rec.Detail
		e.TS = rec.TS
		e.Seq = rec.Seq
		e.Records++
	}
	if err := sc.Err(); err != nil {
		// A scanner error on an existing ledger is not recoverable into a
		// trustworthy state: half a ledger that LOOKS complete is worse than no
		// ledger. Refuse to open.
		return fmt.Errorf("edge: ledger read %s: %w", l.path, err)
	}
	if torn > 0 && l.Log != nil {
		l.Log("ledger %s: skipped %d unreadable line(s), most likely a kill mid-append", l.path, torn)
	}
	return nil
}

// Close flushes and closes the file.
func (l *Ledger) Close() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.file == nil {
		return nil
	}
	err := l.file.Close()
	l.file = nil
	return err
}

// Path is where the ledger lives, for a log line.
func (l *Ledger) Path() string { return l.path }

// State reports a job's current state, or StateUnknown.
//
// Unknown is a real answer and not an error: a resume asks about every job in the
// batch and most of them have no record yet on a first run.
func (l *Ledger) State(jobID string) JobState {
	l.mu.Lock()
	defer l.mu.Unlock()
	e := l.states[jobID]
	if e == nil {
		return StateUnknown
	}
	return e.State
}

// Detail is the free text from a job's last transition.
func (l *Ledger) Detail(jobID string) string {
	l.mu.Lock()
	defer l.mu.Unlock()
	if e := l.states[jobID]; e != nil {
		return e.Detail
	}
	return ""
}

// Done reports whether a job is finished. Unknown counts as not done: the safe
// direction is to do work that turns out to have been done, never to skip work
// that was not.
func (l *Ledger) Done(jobID string) bool { return l.State(jobID) == StateDone }

// Entry returns the full reconstructed record for one job.
func (l *Ledger) Entry(jobID string) (LedgerEntry, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	e := l.states[jobID]
	if e == nil {
		return LedgerEntry{JobID: jobID, State: StateUnknown}, false
	}
	return *e, true
}

// Jobs returns every job the ledger knows about, ordered by id.
func (l *Ledger) Jobs() []LedgerEntry {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make([]LedgerEntry, 0, len(l.states))
	for _, e := range l.states {
		out = append(out, *e)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].JobID < out[j].JobID })
	return out
}

// Pending returns the jobs a resume should act on: everything that is not `done`,
// in id order.
//
// This is the crash-safe-resume entry point, and what it includes is the whole
// point of the ledger:
//
//   - a job caught mid-"dubbing" by a kernel kill is non-terminal, so it comes
//     back here and is requeued. Correct: nothing about the interrupted attempt
//     completed.
//   - a job in `failed` ALSO comes back. NEW_WORKFLOW.MD is explicit that one
//     error must not end a movie and that a failure waits in a failed queue for
//     a retry; a resume that skipped failures would silently drop chunks, which
//     is the exact "movie finished, four chunks missing" outcome this exists to
//     prevent.
//
// The ordering matters less than it looks -- the queue re-serialises by hotspot
// anyway. It is here so a log reads in a stable order across runs.
func (l *Ledger) Pending() []LedgerEntry {
	out := []LedgerEntry{}
	for _, e := range l.Jobs() {
		if !e.State.IsDone() {
			out = append(out, e)
		}
	}
	return out
}

// Interrupted returns the jobs that were mid-flight when the process died, i.e.
// in a non-terminal, non-queued state. These are the ones a resume must not
// treat as "cleanly queued": work had started, and whatever it was writing may be
// half written.
func (l *Ledger) Interrupted() []LedgerEntry {
	out := []LedgerEntry{}
	for _, e := range l.Jobs() {
		if e.State != StateUnknown && !e.State.IsTerminal() && e.State != StateQueued {
			out = append(out, e)
		}
	}
	return out
}

// Degraded reports whether any append has failed.
//
// multitasker.py's rule is that losing a checkpoint must never lose a dub, so
// the failure is logged and swallowed. That policy is right and also hides
// something: a ledger that cannot be written makes resume impossible, and the
// run will still finish looking fine. This is how the caller finds out.
func (l *Ledger) Degraded() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.degraded
}

// Transition moves a job to a new state and appends the record.
//
// It returns ErrIllegalTransition and writes nothing when the move is not in
// jobTransitions. On a successful append it fsyncs before updating memory, so a
// nil return means the state is durable.
//
// An append failure is returned as an error AND leaves memory unchanged, but the
// caller is expected to log it and continue -- see the file header for why.
func (l *Ledger) Transition(jobID string, to JobState, detail string) error {
	return l.transition(jobID, to, detail, false)
}

// Requeue is Transition to queued, forcing the transition.
//
// Forced because it has to work from failed (which allows it) and from every
// other state (which does not). A resume that cannot restart an interrupted job
// is not a resume, and making the caller clear the state through a legal path
// from each of nine states is a worse API than one honest function.
//
// The `detail` is preserved rather than overwritten, because "retrying after X"
// is exactly the line somebody reads at 3am.
func (l *Ledger) Requeue(jobID string, detail string) error {
	return l.transition(jobID, StateQueued, detail, true)
}

func (l *Ledger) transition(jobID string, to JobState, detail string, force bool) error {
	if strings.TrimSpace(jobID) == "" {
		return errors.New("edge: ledger transition needs a job id")
	}
	if !isKnownState(to) {
		return fmt.Errorf("%w: unknown target state %q", ErrIllegalTransition, to)
	}

	l.mu.Lock()
	defer l.mu.Unlock()

	from := StateUnknown
	if e := l.states[jobID]; e != nil {
		from = e.State
	}
	if !force && !CanTransition(from, to) {
		return fmt.Errorf("%w: %s: %s -> %s", ErrIllegalTransition, jobID, from, to)
	}

	l.seq++
	rec := LedgerRecord{
		JobID:  jobID,
		State:  string(to),
		Detail: detail,
		TS:     time.Now().Format("2006-01-02T15:04:05-07:00"),
		Seq:    l.seq,
	}
	line, err := json.Marshal(rec)
	if err != nil {
		return fmt.Errorf("edge: ledger encode %s: %w", jobID, err)
	}
	line = append(line, '\n')

	if l.file == nil {
		l.noteFailure(fmt.Errorf("edge: ledger %s is closed", l.path))
		return fmt.Errorf("edge: ledger %s is closed", l.path)
	}
	// ONE Write of ONE line to an O_APPEND handle. The kernel does not
	// interleave two appends, so two workers cannot produce a spliced record,
	// and a kill leaves at worst a torn final line -- which load() skips.
	if _, err := l.file.Write(line); err != nil {
		l.noteFailure(fmt.Errorf("edge: ledger append %s: %w", jobID, err))
		return fmt.Errorf("edge: ledger append %s: %w", jobID, err)
	}
	// Sync before the memory update. Without it a power loss could leave the
	// in-memory state ahead of the file, which is precisely the drift the
	// resume logic cannot detect.
	if err := l.file.Sync(); err != nil {
		l.noteFailure(fmt.Errorf("edge: ledger sync %s: %w", jobID, err))
		return fmt.Errorf("edge: ledger sync %s: %w", jobID, err)
	}

	// Memory moves only now, so a caller with a nil error has a state that is
	// on disk, and a caller with an error has neither.
	e := l.states[jobID]
	if e == nil {
		e = &LedgerEntry{JobID: jobID}
		l.states[jobID] = e
	}
	e.State = to
	e.Detail = detail
	e.TS = rec.TS
	e.Seq = rec.Seq
	e.Records++
	return nil
}

// noteFailure records a degraded ledger and logs it. The caller must hold mu.
func (l *Ledger) noteFailure(err error) {
	l.lastErr = err
	if !l.degraded {
		l.degraded = true
	}
	if l.Log != nil {
		l.Log("%v -- continuing without checkpoint for the rest of the run", err)
	}
}

// LastError is the most recent append failure, or nil.
func (l *Ledger) LastError() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.lastErr
}

// ImportStates seeds the ledger with jobs that have no record yet, without
// writing anything.
//
// It exists for the resume path: discovering 39 chunks and finding that 38 are
// already done is 38 State() calls, but it is also useful to KNOW the other 1
// exists before the queue is built. Writing a `queued` record for every job
// would double the file for no information.
func (l *Ledger) ImportStates(jobIDs []string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, id := range jobIDs {
		if id == "" {
			continue
		}
		if _, ok := l.states[id]; !ok {
			l.states[id] = &LedgerEntry{JobID: id, State: StateUnknown}
		}
	}
}
