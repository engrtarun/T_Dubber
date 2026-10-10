package edge

// The Go work queue: bounded, pooled, and hotspot-aware.
//
// THE GPU HOTSPOT RULE, AND WHY IT IS SHAPED THIS WAY
// ---------------------------------------------------
// NEW_WORKFLOW.MD's picture is a cluster where GPU 0 runs Homura workers, the
// other GPUs run ASR/TTS, and Go decides where each job goes by looking at
// utilisation, VRAM, queue depth and whether a model is already loaded. That
// needs a scheduler that observes hardware.
//
// Kaggle gives a worker exactly two GPUs, and the notebook already splits them:
// Homura on GPU 0, GPU 1 reserved for ASR and TTS. The interesting part of the
// general rule survives that simplification intact, and it is the part that was
// previously implicit in "run these two things in two threads":
//
//	llm and asr run CONCURRENTLY     -- different GPUs
//	two llm jobs are SERIALISED      -- one GPU, and one model load
//
// "Serialised" is not about throughput here, it is about the model. Loading
// Index-Homura-2B takes seconds and occupies VRAM; two llama-server processes
// each holding a 2B Q4_K_M on the same device is a VRAM overcommit that shows up
// as an OOM kill in the middle of a chunk, long after the cause. Whisper has the
// same property on GPU 1.
//
// So a job carries a HOTSPOT, a job acquires that hotspot's slot before it runs
// and releases it after, and different hotspots do not wait for each other. A
// hotspot with a capacity of 1 is a mutex with a name; a capacity of 4 is four
// ASR workers on one big GPU. The default is 1, because serialising is the
// property that keeps a run from dying and there is no reason to opt out of it
// by accident.
//
// ONE THING TO KNOW BEFORE CHANGING THIS
// ---------------------------------------
// A worker holding a job while it waits for a hotspot slot is a worker doing
// nothing. With Workers >= the number of distinct hotspots that never bites in
// practice; below it, hotspot contention eats the pool. NewQueue defaults
// Workers to 4 and DefaultHotspotCapacity to {llm:1, asr:1}, which gives one
// worker per lane plus slack.
//
// BOUNDED, NOT BLOCKED-FOREVER
// ---------------------------
// Submit blocks when the queue is full and respects ctx while it waits. That is
// deliberate: an unbounded queue in front of a GPU turns "the run is behind" into
// "the run holds 100 GB of video in RAM". TrySubmit is there for a caller that
// wants to drop rather than wait.
//
// ONCE ACCEPTED, A JOB RUNS
// -------------------------
// The context handed to Run is the queue's lifetime, not the caller's. Cancelling
// Submit's context stops the WAIT for room, not the work: abandoning a chunk
// halfway is the failure mode the ledger exists to prevent. A caller that really
// wants to abandon work cancels from inside Run, where it can record why.

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

// Hotspot keys. These match the GgufTask values on purpose, because a job's
// default hotspot IS its task -- "an LLM job runs on the LLM GPU" is the whole
// rule, and making a caller restate it as a bare string is a chance to get it
// wrong.
const (
	// HotspotLLM is GPU 0: the translation model.
	HotspotLLM = "llm"
	// HotspotASR is GPU 1: whisper.cpp.
	HotspotASR = "asr"
)

// Errors from the queue.
var (
	// ErrQueueFull means TrySubmit found no room. Submit waits instead of
	// returning this.
	ErrQueueFull = errors.New("edge: work queue is full")

	// ErrQueueClosed means a job was submitted after Close. Returning this rather
	// than accepting the job and never running it is the difference between a
	// clear bug and a dub that silently loses its last chunk.
	ErrQueueClosed = errors.New("edge: work queue is closed")

	// ErrJobIncomplete means a job had no Run function, so accepting it would
	// mean reporting success for work that never happened.
	ErrJobIncomplete = errors.New("edge: job has no Run function")

	// ErrJobAlreadyDone means the ledger says this job finished. Not a failure
	// of anything: it is how a resume skips work. To run it anyway, Requeue
	// first.
	ErrJobAlreadyDone = errors.New("edge: job already done per ledger")

	// ErrDuplicateJob means the same id was submitted twice to one queue. Two
	// jobs under one id produce a ledger that records one of them and a run that
	// did both, and the mismatch only surfaces later, when somebody reads the
	// ledger and believes it.
	ErrDuplicateJob = errors.New("edge: job id already submitted to this queue")
)

// HotspotDefault is the lane for a job that names no task and no hotspot.
//
// Exclusive like any other. An untagged job is not a free job -- it is a job
// whose resource nobody has claimed, and letting it run alongside everything else
// would be a silent opt-out of the rule that keeps a run alive.
const HotspotDefault = "default"

// Job is one unit of work.
type Job struct {
	// ID is the ledger key. Required, and unique within one queue.
	ID string

	// Task is llm or asr. It decides the default hotspot. Empty falls through to
	// HotspotDefault.
	Task string

	// Hotspot overrides Task for the concurrency decision. This is where "pin
	// this to GPU 1 regardless of what it is" lives, which is the one escape
	// hatch a two-GPU box needs.
	Hotspot string

	// Detail is free text, carried into the ledger record so a state line is
	// self-describing.
	Detail string

	// Run is the work. A returned error fails this job; it does not stop the run.
	Run func(ctx context.Context, j Job) error

	// Seq is the submission index, assigned by the queue. Not set by callers: a
	// caller-supplied value would collide with the queue's own ordering and the
	// results would come back in the wrong order. It rides on the job so Run can
	// log where it sits in the batch.
	Seq int
}

// EffectiveHotspot is the lane this job actually competes for.
func (j Job) EffectiveHotspot() string {
	switch {
	case j.Hotspot != "":
		return j.Hotspot
	case j.Task != "":
		return j.Task
	default:
		return HotspotDefault
	}
}

// Validate rejects a job that cannot be run.
//
// Checked at Submit rather than at execution so the caller finds out while it
// still has the job in hand, instead of a worker discovering it 39 chunks later.
func (j Job) Validate() error {
	if j.ID == "" {
		return errors.New("edge: job needs an id")
	}
	if j.Run == nil {
		return fmt.Errorf("%w: %s", ErrJobIncomplete, j.ID)
	}
	return nil
}

// Result is what happened to one job.
type Result struct {
	Job     Job
	Err     error
	Hotspot string
	// Started and Finished bracket the actual Run, NOT the time the job spent
	// queued. Queue time is a scheduling property; a duration that includes
	// waiting is how "translation is slow" gets claimed about a job that was
	// blocked behind another one.
	Started  time.Time
	Finished time.Time
	// Seq is the submission index, so results come back in submission order
	// rather than completion order.
	Seq int
}

// Duration is wall time inside Run, or 0 for a job that never started.
func (r Result) Duration() time.Duration {
	if r.Started.IsZero() || r.Finished.IsZero() {
		return 0
	}
	return r.Finished.Sub(r.Started)
}

// QueueConfig configures a Queue.
type QueueConfig struct {
	// Workers is the goroutine pool size. Default 4. Should be at least the
	// number of distinct hotspots, or hotspot waits eat the pool.
	Workers int

	// Capacity bounds the queue. Default Workers*4, minimum 1. This is the
	// memory bound: every queued job holds whatever its caller attached.
	Capacity int

	// HotspotCapacity maps a hotspot to how many jobs may hold it at once.
	// Unlisted hotspots get 1.
	HotspotCapacity map[string]int

	// Ledger, when non-nil, records queued -> done/failed. A job already `done`
	// in the ledger is refused by Submit, which is what makes a resumed run
	// cheap.
	Ledger *Ledger

	// OnDone is called once per job, after the ledger record. nil discards.
	OnDone func(Result)

	// OnLedgerError is called when a ledger transition fails. The ledger keeps
	// going regardless -- multitasker.py's rule, and it is right -- but this is
	// how the caller finds out that resume is no longer on the table.
	OnLedgerError func(Job, error)

	// Logger receives queue lifecycle lines. nil discards.
	Logger func(format string, args ...any)
}

// DefaultHotspotCapacity is the split the notebook already uses: one LLM, one
// ASR, each exclusive, the two concurrent with each other.
var DefaultHotspotCapacity = map[string]int{
	HotspotLLM: 1,
	HotspotASR: 1,
}

// Queue is a bounded, pooled, hotspot-serialised work queue.
//
// TWO LOCKS, ON PURPOSE
// ---------------------
//
//	mu    short critical sections only: results, hotspot counters, semaphores.
//	      Never held across a channel send or a callback.
//	subMu held across a submission: the closed check, the pool launch, the
//	      WaitGroup increment, the ledger write and the hand-off, in that order.
//
// subMu exists because of a race that is easy to write and hard to see. A
// submission has to be ordered as
//
//	subMu: not closed -> pool.Add -> queue.Add(1) -> ledger -> send -> unlock
//
// and Close as
//
//	subMu: closed = true, close(work) -> unlock -> queue.Wait(), pool.Wait()
//
// so that a submitter either gets in before Close, and everything it started is
// accounted for, or is turned away. Checking `closed` under mu and sending
// outside it -- the obvious arrangement -- panics with "send on closed channel"
// the moment a run is closed while a producer is blocked on backpressure, and
// backpressure in front of a bounded queue is the normal case, not the rare one.
//
// The pool is launched inside the same section for the same reason: WaitGroup
// Add must not race Wait, and doing both under subMu is what orders them.
//
// Nothing takes mu and then subMu, or subMu and then mu, so there is no lock
// order to get wrong.
type Queue struct {
	cfg   QueueConfig
	work  chan Job
	pool  sync.WaitGroup
	queue sync.WaitGroup

	mu      sync.Mutex
	results map[int]Result
	slots   map[string]chan struct{}
	active  map[string]int
	running int
	// maxPerHotspot is the high-water mark actually observed. It ships with the
	// mechanism on purpose: a future change that quietly breaks serialisation
	// then shows up as a number in a log, not as an unexplained slowdown.
	maxPerHotspot map[string]int

	subMu     sync.Mutex
	closed    bool
	started   bool
	seq       int
	submitted map[string]int // job id -> seq
}

// NewQueue builds a queue. It does not start workers; Start or the first
// submission does.
func NewQueue(cfg QueueConfig) (*Queue, error) {
	if cfg.Workers < 0 {
		return nil, fmt.Errorf("edge: queue workers must be >= 0, got %d", cfg.Workers)
	}
	if cfg.Capacity < 0 {
		return nil, fmt.Errorf("edge: queue capacity must be >= 0, got %d", cfg.Capacity)
	}
	if cfg.Workers == 0 {
		cfg.Workers = 4
	}
	if cfg.Capacity == 0 {
		cfg.Capacity = cfg.Workers * 4
	}
	if cfg.Capacity < 1 {
		cfg.Capacity = 1
	}
	// A hotspot nobody can enter is a deadlock, so capacity 0 is refused rather
	// than defaulted. A caller should have to be loud about asking for one.
	for name, n := range cfg.HotspotCapacity {
		if n < 1 {
			return nil, fmt.Errorf("edge: hotspot %q has capacity %d; use a positive number "+
				"(one exclusive lane is 1, more GPUs is more)", name, n)
		}
	}

	q := &Queue{
		cfg:           cfg,
		submitted:     map[string]int{},
		results:       map[int]Result{},
		slots:         map[string]chan struct{}{},
		active:        map[string]int{},
		maxPerHotspot: map[string]int{},
	}
	q.work = make(chan Job, cfg.Capacity)
	return q, nil
}

// Start launches the worker pool. Calling it twice is a no-op rather than an
// error: a second launch would double the pool and quietly halve the hotspot
// throughput the caller believes it configured.
func (q *Queue) Start() {
	q.subMu.Lock()
	n := 0
	if !q.started && !q.closed {
		q.started = true
		n = q.cfg.Workers
		for i := 0; i < n; i++ {
			q.pool.Add(1)
			go q.worker()
		}
	}
	q.subMu.Unlock()
	if n > 0 {
		q.logf("queue started: %d worker(s), capacity %d, hotspots %v", n, cap(q.work), q.hotspotCapacity())
	}
}

// Submit enqueues a job, waiting for room while the queue is full.
//
// It returns when there is space, when ctx is done, or when the queue is closed.
// The ledger is consulted first: a job already `done` is refused with
// ErrJobAlreadyDone without occupying a slot, because the entire point of the
// ledger is that a resumed run does not redo finished work.
//
// Cancelling ctx stops the WAIT for room, not the work. Once accepted a job runs;
// see the file header.
func (q *Queue) Submit(ctx context.Context, j Job) error {
	j, err := q.admit(j)
	if err != nil {
		return err
	}
	return q.handOff(ctx, j, true)
}

// TrySubmit enqueues without waiting.
//
// ErrQueueFull when there is no room. The ledger is consulted exactly as in
// Submit, so "no room" and "nothing to do" stay distinguishable.
func (q *Queue) TrySubmit(j Job) error {
	j, err := q.admit(j)
	if err != nil {
		return err
	}
	return q.handOff(context.Background(), j, false)
}

// admit validates, consults the ledger, refuses a duplicate id, and stamps the
// job with its submission index. It does not touch the channel.
func (q *Queue) admit(j Job) (Job, error) {
	if err := j.Validate(); err != nil {
		return j, err
	}
	if q.cfg.Ledger != nil && q.cfg.Ledger.Done(j.ID) {
		return j, fmt.Errorf("%w: %s", ErrJobAlreadyDone, j.ID)
	}

	q.subMu.Lock()
	defer q.subMu.Unlock()
	if q.closed {
		return j, fmt.Errorf("%w: %s", ErrQueueClosed, j.ID)
	}
	if _, dup := q.submitted[j.ID]; dup {
		return j, fmt.Errorf("%w: %s", ErrDuplicateJob, j.ID)
	}
	j.Seq = q.seq
	q.seq++
	q.submitted[j.ID] = j.Seq
	return j, nil
}

// handOff moves an admitted job onto the work channel.
//
// Everything from the pool launch through the send happens under subMu, for the
// reasons in the Queue doc comment. The ledger write is inside that window too,
// and it has to be: a worker that finished instantly could otherwise record
// `done` before the submitter records `queued`, leaving the ledger claiming the
// job is waiting when it actually finished. A resume would then redo it.
func (q *Queue) handOff(ctx context.Context, j Job, blocking bool) error {
	q.subMu.Lock()

	if q.closed {
		q.subMu.Unlock()
		return fmt.Errorf("%w: %s", ErrQueueClosed, j.ID)
	}

	// Starting the pool here rather than requiring an explicit Start: a queue
	// that accepts jobs nobody will pick up is the worst of the three options,
	// and Close-without-Start would otherwise return instantly and look like a
	// clean run that did nothing.
	if !q.started {
		q.started = true
		for i := 0; i < q.cfg.Workers; i++ {
			q.pool.Add(1)
			go q.worker()
		}
	}

	// Counted before the send. Close waits on this, so a counter incremented
	// after the hand-off races: Close can observe zero, return, and leave a job
	// running with nothing waiting for it.
	q.queue.Add(1)

	if q.cfg.Ledger != nil {
		q.transition(j, StateQueued, "")
	}

	if !blocking {
		select {
		case q.work <- j:
			q.subMu.Unlock()
			return nil
		default:
			q.queue.Done()
			// The job never entered the queue, so it must not be left recorded as
			// `queued`. `failed` is the honest state: nothing ran, nothing is
			// lost, and Ledger.Pending() picks it up on the next resume.
			if q.cfg.Ledger != nil {
				q.transition(j, StateFailed, "queue full, never started")
			}
			q.subMu.Unlock()
			return fmt.Errorf("%w: %s", ErrQueueFull, j.ID)
		}
	}

	select {
	case q.work <- j:
		q.subMu.Unlock()
		return nil
	case <-ctx.Done():
		// Left recorded as `queued`, deliberately. The job was accepted and never
		// ran, which is exactly what a non-terminal state means, and
		// Ledger.Pending() will hand it back to the next resume. Marking it
		// `failed` would be tidier and would also DROP a chunk the caller still
		// owns.
		q.queue.Done()
		q.subMu.Unlock()
		return ctx.Err()
	}
}

// Close stops accepting work, waits for everything already submitted, and
// returns the results in submission order.
//
// It is idempotent. Close does not cancel in-flight work: a chunk that is 90%
// through should finish, because abandoning it is exactly the "one error loses a
// whole movie" behaviour NEW_WORKFLOW.MD rules out. Cancelling is the caller's
// decision, taken from inside Run.
//
// A caller that gives up on the wait gets the results that DID complete rather
// than an empty set: a partial list is still accurate, and an empty one hides the
// work that finished.
func (q *Queue) Close(ctx context.Context) []Result {
	q.subMu.Lock()
	if !q.closed {
		q.closed = true
		close(q.work)
	}
	q.subMu.Unlock()

	done := make(chan struct{})
	go func() {
		q.queue.Wait()
		q.pool.Wait()
		close(done)
	}()

	select {
	case <-done:
	case <-ctx.Done():
	}

	q.mu.Lock()
	out := make([]Result, 0, len(q.results))
	for _, r := range q.results {
		out = append(out, r)
	}
	q.mu.Unlock()
	sort.Slice(out, func(i, j int) bool { return out[i].Seq < out[j].Seq })
	return out
}

// Stats is a point-in-time view of the queue, for a health line. The maps are
// copies.
type Stats struct {
	Queued        int
	Running       int
	PerHotspot    map[string]int
	MaxPerHotspot map[string]int
	Completed     int
	Failed        int
	Capacity      int
	Workers       int
}

// Stats returns the current counters.
func (q *Queue) Stats() Stats {
	q.mu.Lock()
	defer q.mu.Unlock()
	st := Stats{
		Queued:        len(q.work),
		Running:       q.running,
		PerHotspot:    make(map[string]int, len(q.active)),
		MaxPerHotspot: make(map[string]int, len(q.maxPerHotspot)),
		Capacity:      cap(q.work),
		Workers:       q.cfg.Workers,
	}
	for k, v := range q.active {
		st.PerHotspot[k] = v
	}
	for k, v := range q.maxPerHotspot {
		st.MaxPerHotspot[k] = v
	}
	for _, r := range q.results {
		if r.Err != nil {
			st.Failed++
		} else {
			st.Completed++
		}
	}
	return st
}

// MaxConcurrencyFor is the highest number of jobs that were ever holding a
// hotspot at once. Asserted by the tests, and worth logging in production: a
// value above the configured capacity means the serialisation is not holding.
func (q *Queue) MaxConcurrencyFor(hotspot string) int {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.maxPerHotspot[hotspot]
}

func (q *Queue) worker() {
	defer q.pool.Done()
	for j := range q.work {
		q.runOne(j)
	}
}

// runOne takes the hotspot slot, runs, and releases it.
func (q *Queue) runOne(j Job) {
	defer q.queue.Done()

	hotspot := j.EffectiveHotspot()
	slot := q.slot(hotspot)
	// Unconditional acquire: a job that has been accepted runs. Abandoning it
	// here is the failure the ledger cannot repair, and no caller has asked for
	// that.
	slot <- struct{}{}
	q.enterHotspot(hotspot)

	start := time.Now()
	var runErr error
	func() {
		// Released before the result plumbing below, so a panic in there still
		// returns the slot. An unbalanced semaphore is the bug that shows up as
		// the second chunk hanging forever, in no log at all.
		defer q.leaveHotspot(hotspot)
		defer func() { <-slot }()
		runErr = j.Run(context.Background(), j)
	}()
	q.finish(j, hotspot, runErr, start, time.Now())
}

func (q *Queue) finish(j Job, hotspot string, runErr error, start, end time.Time) {
	if q.cfg.Ledger != nil {
		to, detail := StateDone, j.Detail
		if runErr != nil {
			to, detail = StateFailed, runErr.Error()
		}
		q.transition(j, to, detail)
	}

	res := Result{Job: j, Err: runErr, Hotspot: hotspot, Started: start, Finished: end, Seq: j.Seq}
	q.mu.Lock()
	q.running--
	q.results[j.Seq] = res
	q.mu.Unlock()

	if q.cfg.OnDone != nil {
		q.cfg.OnDone(res)
	}
	if runErr != nil {
		// Logged, not fatal. NEW_WORKFLOW.MD's rule, verbatim: one error is not
		// the loss of a whole movie. The rest keep going and this one waits in a
		// failed queue for its retry.
		q.logf("job %s (%s) failed: %v", j.ID, hotspot, runErr)
	}
}

func (q *Queue) transition(j Job, to JobState, detail string) {
	err := q.cfg.Ledger.Transition(j.ID, to, detail)
	if err == nil {
		return
	}
	// Both kinds are reported and neither is fatal. An illegal transition is a
	// caller bug; a write failure means resume is no longer possible. The run
	// continues either way, because the alternative is losing the dub.
	if q.cfg.OnLedgerError != nil {
		q.cfg.OnLedgerError(j, err)
	}
	q.logf("ledger transition for %s -> %s failed: %v", j.ID, to, err)
}

// slot returns the semaphore for a hotspot, creating it on first use.
//
// Created under q.mu, because two jobs racing on a new hotspot would otherwise
// both see a nil entry and end up with two semaphores -- which silently disables
// the serialisation this whole file exists to provide.
func (q *Queue) slot(hotspot string) chan struct{} {
	q.mu.Lock()
	defer q.mu.Unlock()
	if s, ok := q.slots[hotspot]; ok {
		return s
	}
	n := 1
	if c, ok := q.cfg.HotspotCapacity[hotspot]; ok {
		n = c
	}
	s := make(chan struct{}, n)
	q.slots[hotspot] = s
	return s
}

func (q *Queue) enterHotspot(h string) {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.active[h]++
	q.running++
	if q.active[h] > q.maxPerHotspot[h] {
		q.maxPerHotspot[h] = q.active[h]
	}
}

func (q *Queue) leaveHotspot(h string) {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.active[h]--
}

func (q *Queue) hotspotCapacity() map[string]int {
	out := map[string]int{}
	for k, v := range DefaultHotspotCapacity {
		out[k] = v
	}
	for k, v := range q.cfg.HotspotCapacity {
		out[k] = v
	}
	return out
}

func (q *Queue) logf(format string, args ...any) {
	if q.cfg.Logger != nil {
		q.cfg.Logger(format, args...)
	}
}
