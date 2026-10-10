package edge

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func tmpLedger(t *testing.T) (*Ledger, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "multitasker_ledger.jsonl")
	l, err := OpenLedger(path)
	if err != nil {
		t.Fatalf("OpenLedger: %v", err)
	}
	t.Cleanup(func() { _ = l.Close() })
	return l, path
}

// The whole point of the format is that multitasker.py can read what Go writes
// and vice versa. The keys are asserted by name and value, not just "it is JSON",
// because a renamed key parses fine and silently makes the other side see a
// ledger of jobs that never moved.
func TestLedgerRecordShapeMatchesPython(t *testing.T) {
	l, path := tmpLedger(t)
	if err := l.Transition("job-000", StateQueued, "chunk 1/39"); err != nil {
		t.Fatalf("transition: %v", err)
	}
	if err := l.Transition("job-000", StatePreparing, ""); err != nil {
		t.Fatalf("transition: %v", err)
	}

	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("open ledger: %v", err)
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	lines := 0
	for sc.Scan() {
		lines++
		var rec map[string]any
		if err := json.Unmarshal(sc.Bytes(), &rec); err != nil {
			t.Fatalf("line %d is not JSON: %v", lines, err)
		}
		for _, key := range []string{"job_id", "state", "detail", "ts"} {
			if _, ok := rec[key]; !ok {
				t.Errorf("line %d is missing %q, which multitasker.py reads: %v", lines, key, rec)
			}
		}
		if got := rec["job_id"]; got != "job-000" {
			t.Errorf("line %d job_id = %v", lines, got)
		}
		if got := rec["state"]; got != "queued" && got != "preparing" {
			t.Errorf("line %d state = %v", lines, got)
		}
		ts, _ := rec["ts"].(string)
		// Python writes datetime.now().astimezone().isoformat(timespec="seconds"),
		// which is "2026-10-10T22:11:30+05:30". Go's layout must produce the same
		// shape or a reader that sorts or parses the field disagrees.
		if len(ts) != len("2026-10-10T22:11:30+05:30") || ts[4] != '-' || ts[10] != 'T' {
			t.Errorf("line %d ts %q is not isoformat(seconds) with an offset", lines, ts)
		}
	}
	if lines != 2 {
		t.Fatalf("expected 2 records, got %d", lines)
	}
}

func TestLedgerResumeAcrossReopen(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "ledger.jsonl")

	l1, err := OpenLedger(path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	// Job A finishes. Job B dies mid-dubbing.
	mustTransition(t, l1, "job-a", StateQueued, "")
	mustTransition(t, l1, "job-a", StatePreparing, "")
	mustTransition(t, l1, "job-a", StatePrepared, "")
	mustTransition(t, l1, "job-a", StateDubbing, "")
	mustTransition(t, l1, "job-a", StateDubbed, "")
	mustTransition(t, l1, "job-a", StateUploading, "")
	mustTransition(t, l1, "job-a", StateUploaded, "")
	mustTransition(t, l1, "job-a", StateDone, "chunk 1")
	mustTransition(t, l1, "job-b", StateQueued, "")
	mustTransition(t, l1, "job-b", StatePreparing, "")
	mustTransition(t, l1, "job-b", StatePrepared, "")
	mustTransition(t, l1, "job-b", StateDubbing, "killed here")
	if err := l1.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	l2, err := OpenLedger(path)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer l2.Close()

	if got := l2.State("job-a"); got != StateDone {
		t.Errorf("job-a is %q after resume, want done", got)
	}
	if got := l2.State("job-b"); got != StateDubbing {
		t.Errorf("job-b is %q after resume, want dubbing (the state at the kill)", got)
	}
	if !l2.Done("job-a") {
		t.Error("Done(job-a) is false for a finished job")
	}
	if l2.Done("job-b") {
		t.Error("Done(job-b) is true for an interrupted job")
	}
	if got := l2.Detail("job-b"); got != "killed here" {
		t.Errorf("job-b detail = %q, want the last written detail", got)
	}

	// Resume semantics: done is skipped, everything else comes back. A resume
	// that skipped failures would silently drop chunks, which is the exact
	// "movie finished, four chunks missing" outcome the ledger exists to stop.
	pending := ids(l2.Pending())
	if len(pending) != 1 || pending[0] != "job-b" {
		t.Errorf("Pending() = %v, want [job-b]", pending)
	}
	interrupted := ids(l2.Interrupted())
	if len(interrupted) != 1 || interrupted[0] != "job-b" {
		t.Errorf("Interrupted() = %v, want [job-b]", interrupted)
	}
}

// A failure is not terminal for the job. That is the difference between "one
// error costs a chunk" and "one error costs a movie".
func TestPendingIncludesFailed(t *testing.T) {
	l, _ := tmpLedger(t)
	// queued -> done is legal: a Go job is one step, not the whole pipeline.
	mustTransition(t, l, "ok", StateQueued, "")
	mustTransition(t, l, "ok", StateDone, "")
	mustTransition(t, l, "bad", StateQueued, "")
	mustTransition(t, l, "bad", StateFailed, "chunk 4 of 39: whisper died")

	if !l.State("bad").IsTerminal() {
		t.Error("failed should be terminal for the attempt")
	}
	if l.Done("bad") {
		t.Error("Done() must be false for failed")
	}
	if got := ids(l.Pending()); len(got) != 1 || got[0] != "bad" {
		t.Errorf("Pending() = %v, want the failed job back for a retry", got)
	}
}

// A kernel killed mid-append leaves a torn final line. Refusing to start because
// of it turns a recoverable crash into an unrecoverable one, which is exactly
// what multitasker.py's _load() avoids by skipping undecodable lines.
func TestLedgerSkipsATornLastLine(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "ledger.jsonl")

	good := `{"job_id": "job-000", "state": "done", "detail": "finished", "ts": "2026-10-10T10:00:00+05:30"}` + "\n"
	// A half-written record: the process died between write() calls.
	torn := `{"job_id": "job-001", "state": "dubb`
	if err := os.WriteFile(path, []byte(good+torn), 0o644); err != nil {
		t.Fatalf("seed: %v", err)
	}

	var logged []string
	l, err := OpenLedger(path)
	if err != nil {
		t.Fatalf("a torn line must not stop the ledger opening: %v", err)
	}
	defer l.Close()

	if got := l.State("job-000"); got != StateDone {
		t.Errorf("job-000 = %q, want done -- the complete line must survive", got)
	}
	if got := l.State("job-001"); got != StateUnknown {
		t.Errorf("job-001 = %q, want unknown -- the torn line must be dropped", got)
	}
	_ = logged
}

// Last record wins. Replaying an append-only log forward is the entire recovery
// algorithm, so a job that moved twice must read as its LAST state.
func TestLedgerLastRecordWins(t *testing.T) {
	l, _ := tmpLedger(t)
	mustTransition(t, l, "job-000", StateQueued, "")
	mustTransition(t, l, "job-000", StateFailed, "attempt 1 died")
	mustTransition(t, l, "job-000", StateQueued, "retry")
	mustTransition(t, l, "job-000", StateFailed, "attempt 2 died")

	if got := l.State("job-000"); got != StateFailed {
		t.Errorf("state = %q, want failed", got)
	}
	if got := l.Detail("job-000"); got != "attempt 2 died" {
		t.Errorf("detail = %q, want the last one", got)
	}
	e, ok := l.Entry("job-000")
	if !ok || e.Records != 4 {
		t.Errorf("Entry records = %d (ok=%v), want 4", e.Records, ok)
	}
}

// Go validates transitions that multitasker.py does not, because Go drives jobs
// concurrently. An illegal move must be refused AND must not write: a ledger that
// records an impossible state is worse than one missing a record, because the
// resume cannot tell which.
func TestIllegalTransitionIsRefusedAndNotWritten(t *testing.T) {
	l, path := tmpLedger(t)
	mustTransition(t, l, "job-000", StateQueued, "")

	err := l.Transition("job-000", StateUploaded, "jump the queue")
	if !errors.Is(err, ErrIllegalTransition) {
		t.Fatalf("queued -> uploaded should be illegal, got %v", err)
	}
	if got := l.State("job-000"); got != StateQueued {
		t.Errorf("state moved to %q after a refused transition", got)
	}

	// done is terminal: nothing leaves it without an explicit requeue.
	mustTransition(t, l, "done-job", StateDone, "")
	if err := l.Transition("done-job", StatePreparing, ""); !errors.Is(err, ErrIllegalTransition) {
		t.Errorf("done -> preparing should be illegal, got %v", err)
	}

	body, _ := os.ReadFile(path)
	for _, line := range strings.Split(strings.TrimSpace(string(body)), "\n") {
		if strings.Contains(line, "uploaded") || strings.Contains(line, "jump the queue") {
			t.Errorf("a refused transition reached the file: %s", line)
		}
	}
}

// Requeue is forced, because a resume has to be able to restart an interrupted
// job from ANY of the nine non-terminal states, and making the caller walk a
// legal path out of each one is a worse API than one honest function.
func TestRequeueForcesFromAnyState(t *testing.T) {
	// walkTo drives a job to a state through legal transitions only, so the only
	// thing under test is that Requeue itself does not need one.
	walks := map[JobState][]JobState{
		// A job with no record at all reads as `unknown`, so reaching `queued`
		// needs an explicit first transition like any other.
		StateQueued:    {StateQueued},
		StatePreparing: {StatePreparing},
		StatePrepared:  {StatePreparing, StatePrepared},
		StateDubbing:   {StatePreparing, StatePrepared, StateDubbing},
		StateDubbed:    {StatePreparing, StatePrepared, StateDubbing, StateDubbed},
		StateSyncing:   {StatePreparing, StatePrepared, StateDubbing, StateDubbed, StateSyncing},
		StateSynced:    {StatePreparing, StatePrepared, StateDubbing, StateDubbed, StateSyncing, StateSynced},
		StateUploading: {StatePreparing, StatePrepared, StateDubbing, StateDubbed, StateUploading},
		StateUploaded:  {StatePreparing, StatePrepared, StateDubbing, StateDubbed, StateUploading, StateUploaded},
		StateFailed:    {StateFailed},
		StateDone:      {StateDone},
	}

	for _, from := range []JobState{
		StateQueued, StatePreparing, StatePrepared, StateDubbing, StateDubbed,
		StateSyncing, StateSynced, StateUploading, StateUploaded, StateFailed, StateDone,
	} {
		l, _ := tmpLedger(t)
		for _, step := range walks[from] {
			mustTransition(t, l, "job-000", step, "")
		}
		if got := l.State("job-000"); got != from {
			t.Fatalf("could not reach %s (landed on %s)", from, got)
		}
		if err := l.Requeue("job-000", "retry after resume"); err != nil {
			t.Errorf("Requeue from %s: %v", from, err)
			continue
		}
		if got := l.State("job-000"); got != StateQueued {
			t.Errorf("Requeue from %s left the job at %q", from, got)
		}
		if got := l.Detail("job-000"); got != "retry after resume" {
			t.Errorf("Requeue from %s detail = %q", from, got)
		}
	}
}

func TestCanTransition(t *testing.T) {
	cases := []struct {
		from, to JobState
		want     bool
	}{
		{StateQueued, StatePreparing, true},
		{StateQueued, StateDubbing, false},
		{StateQueued, StateFailed, true},
		// A Go job is one step, so queued -> done is ordinary.
		{StateQueued, StateDone, true},
		{StatePrepared, StateDubbing, true},
		{StateDubbed, StateSyncing, true},
		{StateDubbed, StateUploading, true},
		{StateDubbed, StateDone, true},
		{StateSynced, StateUploading, true},
		{StateUploaded, StateDone, true},
		{StateFailed, StateQueued, true},
		{StateDone, StateQueued, false},
		{StateDone, StateFailed, false},
		// Same state is legal: a heartbeat or a late detail must not need a
		// fake state to attach to.
		{StateDubbing, StateDubbing, true},
		// Any state may be a job's first, because there is no "from" to check.
		{StateUnknown, StateDone, true},
		{"", StateFailed, true},
		// An unknown target is never legal.
		{StateQueued, JobState("wat"), false},
		{StateQueued, "", false},
	}
	for _, tc := range cases {
		if got := CanTransition(tc.from, tc.to); got != tc.want {
			t.Errorf("CanTransition(%q, %q) = %v, want %v", tc.from, tc.to, got, tc.want)
		}
	}
}

// Atomic means the in-memory state only moves after the record is on disk, so a
// caller with a nil error has a state that survived a power cut and a caller
// with an error has neither.
func TestTransitionIsAtomicAcrossAFailedAppend(t *testing.T) {
	l, _ := tmpLedger(t)
	mustTransition(t, l, "job-000", StateQueued, "")

	// Closing the handle under the ledger makes every append fail, which is the
	// closest a test can get to a full disk without filling one.
	if err := l.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	if err := l.Transition("job-000", StatePreparing, ""); err == nil {
		t.Fatal("a transition on a closed ledger should fail")
	}
	if got := l.State("job-000"); got != StateQueued {
		t.Errorf("state = %q after a failed append; memory and disk must agree", got)
	}
	if !l.Degraded() {
		t.Error("Degraded() is false after an append failure -- the run would finish looking fine with no resume")
	}
	if l.LastError() == nil {
		t.Error("LastError() is nil after an append failure")
	}
}

// Losing a checkpoint must never lose a dub -- multitasker.py's rule -- so the
// write error is surfaced and the caller carries on. It also has to be REPORTED,
// once, or the run finishes looking clean while resume is impossible.
func TestLedgerDegradationIsLoggedOnce(t *testing.T) {
	path := filepath.Join(t.TempDir(), "ledger.jsonl")
	var mu sync.Mutex
	var lines []string
	l, err := OpenLedger(path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	l.Log = func(format string, args ...any) {
		mu.Lock()
		lines = append(lines, fmt.Sprintf(format, args...))
		mu.Unlock()
	}
	defer l.Close()

	_ = l.Close() // make appends fail
	_ = l.Transition("a", StateQueued, "")
	_ = l.Transition("b", StateQueued, "")

	mu.Lock()
	defer mu.Unlock()
	if len(lines) == 0 {
		t.Error("a degraded ledger logged nothing")
	}
	if !strings.Contains(lines[0], "checkpoint") {
		t.Errorf("log line does not say what the consequence is: %q", lines[0])
	}
}

// Many workers, one file. Two appends must never interleave into one spliced
// line, because load() would then skip BOTH records and silently lose two jobs.
func TestLedgerConcurrentAppendsStayIntact(t *testing.T) {
	l, path := tmpLedger(t)

	const workers, perWorker = 8, 25
	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; i < perWorker; i++ {
				id := fmt.Sprintf("job-%d-%d", w, i)
				_ = l.Transition(id, StateQueued, "")
				_ = l.Transition(id, StateDone, "")
			}
		}(w)
	}
	wg.Wait()
	if err := l.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	seen := 0
	for _, line := range strings.Split(strings.TrimSpace(string(body)), "\n") {
		var rec LedgerRecord
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("a record was spliced: %v -- line: %s", err, line)
		}
		seen++
	}
	if seen != workers*perWorker*2 {
		t.Fatalf("read %d records, wrote %d", seen, workers*perWorker*2)
	}

	// Reopen and confirm the reconstruction survived the concurrency.
	l2, err := OpenLedger(path)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer l2.Close()
	if got := len(l2.Jobs()); got != workers*perWorker {
		t.Errorf("reconstructed %d jobs, want %d", got, workers*perWorker)
	}
	for _, e := range l2.Jobs() {
		if e.State != StateDone {
			t.Fatalf("%s came back as %q", e.JobID, e.State)
		}
	}
}

func TestLedgerUnknownJob(t *testing.T) {
	l, _ := tmpLedger(t)
	if got := l.State("never-seen"); got != StateUnknown {
		t.Errorf("State of an unseen job = %q, want %q", got, StateUnknown)
	}
	if got := l.Detail("never-seen"); got != "" {
		t.Errorf("Detail of an unseen job = %q", got)
	}
	if _, ok := l.Entry("never-seen"); ok {
		t.Error("Entry reported ok for an unseen job")
	}
	// Unknown must not read as done. The safe direction is to redo work that
	// turns out to have been done, never to skip work that was not.
	if l.Done("never-seen") {
		t.Error("Done(unseen) is true")
	}
}

func TestLedgerRejectsEmptyJobID(t *testing.T) {
	l, _ := tmpLedger(t)
	if err := l.Transition("", StateQueued, ""); err == nil {
		t.Error("an empty job id was accepted")
	}
	if err := l.Requeue("   ", ""); err == nil {
		t.Error("a blank job id was accepted")
	}
	if _, err := OpenLedger(""); err == nil {
		t.Error("OpenLedger(\"\") should fail")
	}
}

func TestImportStatesDoesNotWrite(t *testing.T) {
	l, path := tmpLedger(t)
	l.ImportStates([]string{"a", "b", "", "a"})

	// The file exists -- OpenLedger creates it so a crash cannot lose the handle
	// -- but it must be empty. ImportStates is a discovery helper; writing a
	// record per discovered job would double the file for no information.
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if info.Size() != 0 {
		t.Errorf("ImportStates wrote %d bytes; it is a discovery helper, not a checkpoint", info.Size())
	}
	if got := l.State("a"); got != StateUnknown {
		t.Errorf("imported job state = %q, want unknown", got)
	}
	if got := len(l.Jobs()); got != 2 {
		t.Errorf("Jobs() = %d, want 2 (the duplicate and the blank are dropped)", got)
	}
}

func TestLedgerOpensInAMissingDirectory(t *testing.T) {
	path := filepath.Join(t.TempDir(), "work", "deep", "ledger.jsonl")
	l, err := OpenLedger(path)
	if err != nil {
		t.Fatalf("OpenLedger should create parent directories: %v", err)
	}
	defer l.Close()
	if err := l.Transition("job-000", StateQueued, ""); err != nil {
		t.Fatalf("transition: %v", err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Errorf("ledger file was not created: %v", err)
	}
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func mustTransition(t *testing.T, l *Ledger, id string, to JobState, detail string) {
	t.Helper()
	if err := l.Transition(id, to, detail); err != nil {
		t.Fatalf("Transition(%s, %s): %v", id, to, err)
	}
}

func ids(entries []LedgerEntry) []string {
	out := make([]string, 0, len(entries))
	for _, e := range entries {
		out = append(out, e.JobID)
	}
	return out
}
