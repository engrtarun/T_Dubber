package edge

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// concurrencyCounter is a tiny "how many of these were ever in flight at once"
// meter. It is a high-water mark rather than an instantaneous reading, because
// an instantaneous one is taken at whatever moment the test happens to look and
// would pass a broken implementation as often as it failed one.
type concurrencyCounter struct {
	cur int32
	max int32
}

func (c *concurrencyCounter) enter() {
	n := atomic.AddInt32(&c.cur, 1)
	for {
		m := atomic.LoadInt32(&c.max)
		if n <= m || atomic.CompareAndSwapInt32(&c.max, m, n) {
			return
		}
	}
}

func (c *concurrencyCounter) leave() { atomic.AddInt32(&c.cur, -1) }
func (c *concurrencyCounter) peak() int {
	return int(atomic.LoadInt32(&c.max))
}

// sleepJob runs fn under the meter, with a dwell time long enough that a broken
// serialisation has time to show itself on a loaded machine.
func sleepJob(c *concurrencyCounter, dwell time.Duration) func(context.Context, Job) error {
	return func(context.Context, Job) error {
		c.enter()
		defer c.leave()
		time.Sleep(dwell)
		return nil
	}
}

// THE CORE RULE: two llm jobs are serialised.
//
// This is not a throughput preference. Two llama-server processes each holding
// Index-Homura-2B Q4_K_M on GPU 0 is a VRAM overcommit, and it shows up as an OOM
// kill halfway through a chunk rather than as anything that names its cause.
func TestTwoLLMJobsAreSerialised(t *testing.T) {
	q, err := NewQueue(QueueConfig{Workers: 4, Capacity: 16})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	var meter concurrencyCounter

	const n = 6
	start := time.Now()
	for i := 0; i < n; i++ {
		if err := q.Submit(context.Background(), Job{
			ID:   fmt.Sprintf("chunk-%03d", i),
			Task: HotspotLLM,
			Run:  sleepJob(&meter, 40*time.Millisecond),
		}); err != nil {
			t.Fatalf("submit %d: %v", i, err)
		}
	}
	results := q.Close(context.Background())
	elapsed := time.Since(start)

	if len(results) != n {
		t.Fatalf("got %d results, want %d", len(results), n)
	}
	for _, r := range results {
		if r.Err != nil {
			t.Errorf("%s: %v", r.Job.ID, r.Err)
		}
	}
	if peak := meter.peak(); peak != 1 {
		t.Errorf("peak llm concurrency was %d, want 1 -- two LLM jobs ran at once on one GPU", peak)
	}
	if peak := q.MaxConcurrencyFor(HotspotLLM); peak != 1 {
		t.Errorf("queue's own high-water mark says %d, want 1", peak)
	}
	// A second, independent witness. If the meter above were broken, the wall
	// clock still shows the serialisation, because 6 x 40ms cannot finish in
	// less than 240ms when they run one after another.
	if want := n * 40 * time.Millisecond; elapsed < want-10*time.Millisecond {
		t.Errorf("%d llm jobs finished in %s; serialised work cannot beat %s", n, elapsed, want)
	}
}

// THE OTHER HALF OF THE CORE RULE: asr is serialised too, on its own GPU.
func TestTwoASRJobsAreSerialised(t *testing.T) {
	q, err := NewQueue(QueueConfig{Workers: 4, Capacity: 16})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	var meter concurrencyCounter

	for i := 0; i < 5; i++ {
		if err := q.Submit(context.Background(), Job{
			ID:   fmt.Sprintf("asr-%d", i),
			Task: HotspotASR,
			Run:  sleepJob(&meter, 25*time.Millisecond),
		}); err != nil {
			t.Fatalf("submit: %v", err)
		}
	}
	q.Close(context.Background())
	if peak := meter.peak(); peak != 1 {
		t.Errorf("peak asr concurrency was %d, want 1", peak)
	}
}

// ... and an llm job and an asr job run CONCURRENTLY, because they are on
// different GPUs. This is the notebook's "Homura on GPU 0, GPU 1 reserved for
// ASR/TTS" split, expressed as code.
//
// The proof is a rendezvous, not a timing comparison: both jobs must REACH their
// body before either is allowed to finish. If the hotspots serialised each other,
// the second could never get there, and the test would fail on a timeout instead
// of on a flaky millisecond threshold.
func TestLLMAndASRRunConcurrently(t *testing.T) {
	q, err := NewQueue(QueueConfig{Workers: 4, Capacity: 8})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}

	started := make(chan string, 4)
	release := make(chan struct{})
	var once sync.Once
	defer once.Do(func() { close(release) })

	wait := func(id, task string) func(context.Context, Job) error {
		return func(ctx context.Context, j Job) error {
			started <- id
			select {
			case <-release:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		}
	}

	if err := q.Submit(context.Background(), Job{ID: "translate", Task: HotspotLLM, Run: wait("llm", HotspotLLM)}); err != nil {
		t.Fatalf("submit llm: %v", err)
	}
	if err := q.Submit(context.Background(), Job{ID: "transcribe", Task: HotspotASR, Run: wait("asr", HotspotASR)}); err != nil {
		t.Fatalf("submit asr: %v", err)
	}

	for i := 0; i < 2; i++ {
		select {
		case <-started:
		case <-time.After(10 * time.Second):
			t.Fatal("only one of llm/asr ever started: the two hotspots are serialising " +
				"each other, which throws away the second GPU")
		}
	}
	once.Do(func() { close(release) })
	q.Close(context.Background())
}

// An untagged job is not a free job. It lands in one exclusive lane, so it
// cannot quietly opt out of the rule that keeps a run alive.
func TestUntaggedJobsShareOneExclusiveLane(t *testing.T) {
	q, err := NewQueue(QueueConfig{Workers: 4, Capacity: 8})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	var meter concurrencyCounter
	for i := 0; i < 4; i++ {
		if err := q.Submit(context.Background(), Job{
			ID:  fmt.Sprintf("untagged-%d", i),
			Run: sleepJob(&meter, 20*time.Millisecond),
		}); err != nil {
			t.Fatalf("submit: %v", err)
		}
	}
	q.Close(context.Background())
	if peak := meter.peak(); peak != 1 {
		t.Errorf("peak default-lane concurrency was %d, want 1", peak)
	}
	if peak := q.MaxConcurrencyFor(HotspotDefault); peak != 1 {
		t.Errorf("default lane high-water mark %d, want 1", peak)
	}
}

// The escape hatch a two-GPU box needs: pin by hotspot, regardless of task.
func TestHotspotOverridesTask(t *testing.T) {
	j := Job{ID: "x", Task: HotspotLLM, Hotspot: "gpu1"}
	if got := j.EffectiveHotspot(); got != "gpu1" {
		t.Errorf("EffectiveHotspot = %q, want gpu1", got)
	}
	j = Job{ID: "x", Task: HotspotLLM}
	if got := j.EffectiveHotspot(); got != HotspotLLM {
		t.Errorf("EffectiveHotspot = %q, want %q", got, HotspotLLM)
	}
	if got := (Job{ID: "x"}).EffectiveHotspot(); got != HotspotDefault {
		t.Errorf("EffectiveHotspot = %q, want %q", got, HotspotDefault)
	}

	// And two jobs pinned to the same custom hotspot serialise against each
	// other, not against their tasks.
	q, err := NewQueue(QueueConfig{Workers: 4, Capacity: 8})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	var meter concurrencyCounter
	for i := 0; i < 4; i++ {
		if err := q.Submit(context.Background(), Job{
			ID: fmt.Sprintf("pinned-%d", i),
			// Deliberately mixed tasks: same task would serialise for the wrong
			// reason, so this isolates the hotspot as the deciding factor.
			Task:    []string{HotspotLLM, HotspotASR}[i%2],
			Hotspot: "gpu1",
			Run:     sleepJob(&meter, 20*time.Millisecond),
		}); err != nil {
			t.Fatalf("submit: %v", err)
		}
	}
	q.Close(context.Background())
	if peak := meter.peak(); peak != 1 {
		t.Errorf("peak gpu1 concurrency was %d, want 1", peak)
	}
}

// A bigger box widens a lane instead of inventing a new rule: capacity 2 means
// two jobs, which is what "four ASR workers on one large GPU" actually is.
func TestHotspotCapacityAboveOne(t *testing.T) {
	q, err := NewQueue(QueueConfig{
		Workers:         4,
		Capacity:        8,
		HotspotCapacity: map[string]int{HotspotASR: 2},
	})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	var meter concurrencyCounter
	for i := 0; i < 6; i++ {
		if err := q.Submit(context.Background(), Job{
			ID:   fmt.Sprintf("asr-%d", i),
			Task: HotspotASR,
			Run:  sleepJob(&meter, 40*time.Millisecond),
		}); err != nil {
			t.Fatalf("submit: %v", err)
		}
	}
	q.Close(context.Background())
	if peak := meter.peak(); peak != 2 {
		t.Errorf("peak asr concurrency was %d, want 2 -- the configured capacity is ignored", peak)
	}
	if peak := q.MaxConcurrencyFor(HotspotASR); peak != 2 {
		t.Errorf("asr high-water mark %d, want 2", peak)
	}
}

// A hotspot nobody can enter is a deadlock. It is refused at construction rather
// than discovered when every job hangs.
func TestNewQueueRejectsAnImpossibleHotspot(t *testing.T) {
	if _, err := NewQueue(QueueConfig{HotspotCapacity: map[string]int{HotspotLLM: 0}}); err == nil {
		t.Error("a hotspot with capacity 0 was accepted")
	}
	if _, err := NewQueue(QueueConfig{HotspotCapacity: map[string]int{HotspotLLM: -2}}); err == nil {
		t.Error("a hotspot with negative capacity was accepted")
	}
	if _, err := NewQueue(QueueConfig{Workers: -1}); err == nil {
		t.Error("negative workers was accepted")
	}
}

// An unbounded queue in front of a GPU turns "the run is behind" into "the run
// holds 100 GB of video in RAM". Submit blocking is the backpressure.
func TestSubmitBlocksWhenTheQueueIsFull(t *testing.T) {
	q, err := NewQueue(QueueConfig{Workers: 1, Capacity: 1})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}

	running := make(chan struct{})
	release := make(chan struct{})
	var once sync.Once
	defer once.Do(func() { close(release) })

	// Job a occupies the single worker.
	if err := q.Submit(context.Background(), Job{
		ID:   "a",
		Task: HotspotLLM,
		Run: func(context.Context, Job) error {
			close(running)
			<-release
			return nil
		},
	}); err != nil {
		t.Fatalf("submit a: %v", err)
	}
	select {
	case <-running:
	case <-time.After(10 * time.Second):
		t.Fatal("job a never started")
	}

	// Job b takes the one free buffer slot and returns immediately.
	if err := q.Submit(context.Background(), Job{ID: "b", Task: HotspotLLM, Run: sleepJob(&concurrencyCounter{}, time.Millisecond)}); err != nil {
		t.Fatalf("submit b: %v", err)
	}

	// Job c has nowhere to go and must block.
	third := make(chan error, 1)
	go func() {
		third <- q.Submit(context.Background(), Job{ID: "c", Task: HotspotLLM, Run: sleepJob(&concurrencyCounter{}, time.Millisecond)})
	}()

	select {
	case err := <-third:
		t.Fatalf("submit c returned %v while the queue was full; it should have waited", err)
	case <-time.After(200 * time.Millisecond):
	}

	// Release the worker, and c gets in as soon as b drains.
	once.Do(func() { close(release) })
	select {
	case err := <-third:
		if err != nil {
			t.Fatalf("submit c: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("submit c never completed after the worker freed up")
	}
	q.Close(context.Background())
}

// Cancelling the wait for room is different from cancelling the work: the
// context bounds the WAIT, and a job that never entered the queue leaves no
// `queued` record claiming that it did.
func TestSubmitRespectsContextWhileWaitingForRoom(t *testing.T) {
	q, err := NewQueue(QueueConfig{Workers: 1, Capacity: 1})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	release := make(chan struct{})
	running := make(chan struct{})
	var once sync.Once
	defer once.Do(func() { close(release) })

	if err := q.Submit(context.Background(), Job{ID: "a", Run: func(context.Context, Job) error {
		close(running)
		<-release
		return nil
	}}); err != nil {
		t.Fatalf("submit a: %v", err)
	}
	<-running
	if err := q.Submit(context.Background(), Job{ID: "b", Run: sleepJob(&concurrencyCounter{}, time.Millisecond)}); err != nil {
		t.Fatalf("submit b: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if err := q.Submit(ctx, Job{ID: "c", Run: sleepJob(&concurrencyCounter{}, time.Millisecond)}); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("submit c = %v, want DeadlineExceeded", err)
	}

	once.Do(func() { close(release) })
	q.Close(context.Background())
}

func TestTrySubmitReportsAFullQueue(t *testing.T) {
	q, err := NewQueue(QueueConfig{Workers: 1, Capacity: 1})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	release := make(chan struct{})
	running := make(chan struct{})
	var once sync.Once
	defer once.Do(func() { close(release) })

	if err := q.TrySubmit(Job{ID: "a", Run: func(context.Context, Job) error {
		close(running)
		<-release
		return nil
	}}); err != nil {
		t.Fatalf("try a: %v", err)
	}
	<-running
	if err := q.TrySubmit(Job{ID: "b", Run: sleepJob(&concurrencyCounter{}, time.Millisecond)}); err != nil {
		t.Fatalf("try b: %v", err)
	}
	if err := q.TrySubmit(Job{ID: "c", Run: sleepJob(&concurrencyCounter{}, time.Millisecond)}); !errors.Is(err, ErrQueueFull) {
		t.Fatalf("try c = %v, want ErrQueueFull", err)
	}

	once.Do(func() { close(release) })
	results := q.Close(context.Background())
	if len(results) != 2 {
		t.Errorf("ran %d jobs, want 2 (c never entered the queue)", len(results))
	}
}

// NEW_WORKFLOW.MD, verbatim: one error is not the loss of a whole movie. The
// queue keeps going, the failure is reported per job, and nothing about the run
// depends on every job succeeding.
func TestOneFailingJobDoesNotStopTheRun(t *testing.T) {
	q, err := NewQueue(QueueConfig{Workers: 3, Capacity: 16})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	const n = 6
	for i := 0; i < n; i++ {
		i := i
		run := sleepJob(&concurrencyCounter{}, 5*time.Millisecond)
		if i == 3 {
			run = func(context.Context, Job) error {
				return errors.New("whisper.cpp exited 1 on chunk 4")
			}
		}
		if err := q.Submit(context.Background(), Job{ID: fmt.Sprintf("chunk-%02d", i), Task: HotspotLLM, Run: run}); err != nil {
			t.Fatalf("submit %d: %v", i, err)
		}
	}
	results := q.Close(context.Background())

	if len(results) != n {
		t.Fatalf("got %d results, want %d; the queue stopped early", len(results), n)
	}
	failed := 0
	for _, r := range results {
		if r.Job.ID == "chunk-03" {
			if r.Err == nil {
				t.Error("chunk-03 reported success despite returning an error")
			}
			failed++
			continue
		}
		if r.Err != nil {
			t.Errorf("%s: %v", r.Job.ID, r.Err)
		}
	}
	if failed != 1 {
		t.Errorf("%d jobs reported failure, want exactly 1", failed)
	}
}

func TestLedgerIntegration(t *testing.T) {
	l, _ := tmpLedger(t)
	q, err := NewQueue(QueueConfig{Workers: 2, Capacity: 8, Ledger: l})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}

	// A job from a previous run.
	mustTransition(t, l, "already-done", StateQueued, "")
	mustTransition(t, l, "already-done", StateDone, "run 4")
	// A job that failed and should come back.
	mustTransition(t, l, "retry-me", StateQueued, "")
	mustTransition(t, l, "retry-me", StateFailed, "whisper died")

	// The whole reason the ledger exists: a resumed run does not redo finished
	// work. 10 ms of work that never happens.
	if err := q.Submit(context.Background(), Job{ID: "already-done", Run: sleepJob(&concurrencyCounter{}, 10*time.Millisecond)}); !errors.Is(err, ErrJobAlreadyDone) {
		t.Fatalf("a done job was resubmitted: %v", err)
	}
	// ... and a FAILED job is not done, so it is offered again. Skipping it is
	// how a movie "finishes" with chunks missing.
	if err := q.Submit(context.Background(), Job{ID: "retry-me", Run: sleepJob(&concurrencyCounter{}, time.Millisecond)}); err != nil {
		t.Fatalf("a failed job should be resubmittable: %v", err)
	}
	if err := q.Submit(context.Background(), Job{ID: "brand-new", Run: sleepJob(&concurrencyCounter{}, time.Millisecond)}); err != nil {
		t.Fatalf("submit: %v", err)
	}

	results := q.Close(context.Background())
	if len(results) != 2 {
		t.Fatalf("ran %d jobs, want 2", len(results))
	}
	for _, r := range results {
		if r.Err != nil {
			t.Errorf("%s: %v", r.Job.ID, r.Err)
		}
	}
	if got := l.State("already-done"); got != StateDone {
		t.Errorf("already-done is now %q; a refused resubmission must not touch it", got)
	}
	for _, id := range []string{"retry-me", "brand-new"} {
		if got := l.State(id); got != StateDone {
			t.Errorf("%s is %q after running, want done", id, got)
		}
	}
}

func TestLedgerRecordsFailureDetail(t *testing.T) {
	l, _ := tmpLedger(t)
	q, err := NewQueue(QueueConfig{Workers: 2, Capacity: 4, Ledger: l})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	if err := q.Submit(context.Background(), Job{
		ID:     "chunk-4",
		Detail: "00:15:00-00:20:00",
		Run:    func(context.Context, Job) error { return errors.New("CUDA out of memory") },
	}); err != nil {
		t.Fatalf("submit: %v", err)
	}
	q.Close(context.Background())

	if got := l.State("chunk-4"); got != StateFailed {
		t.Errorf("state = %q, want failed", got)
	}
	if got := l.Detail("chunk-4"); got != "CUDA out of memory" {
		t.Errorf("detail = %q, want the error text; a failed row with no reason is unreadable", got)
	}
}

// Losing a checkpoint must not lose a dub, and must be reported. The queue
// continues, the caller is told, and the job still reports its own result.
func TestLedgerErrorsDoNotStopTheQueue(t *testing.T) {
	l, _ := tmpLedger(t)
	var reported int32
	newQ := func() *Queue {
		q, err := NewQueue(QueueConfig{
			Workers:         2,
			Capacity:        4,
			Ledger:          l,
			OnLedgerError:   func(Job, error) { atomic.AddInt32(&reported, 1) },
			HotspotCapacity: map[string]int{HotspotLLM: 1},
		})
		if err != nil {
			t.Fatalf("NewQueue: %v", err)
		}
		return q
	}

	// First, the happy path, so the counter is known to start clean.
	q := newQ()
	if err := q.Submit(context.Background(), Job{ID: "a", Task: HotspotLLM, Run: sleepJob(&concurrencyCounter{}, time.Millisecond)}); err != nil {
		t.Fatalf("submit: %v", err)
	}
	q.Close(context.Background())
	if got := atomic.LoadInt32(&reported); got != 0 {
		t.Fatalf("OnLedgerError fired %d times on a healthy ledger", got)
	}

	// Break the ledger, then run a second queue over it. A queue is one-shot --
	// Close ends it for good -- so "the ledger died mid-run" needs a fresh queue,
	// not a reused one.
	_ = l.Close()

	q2 := newQ()
	var meter concurrencyCounter
	for i := 0; i < 3; i++ {
		if err := q2.Submit(context.Background(), Job{ID: fmt.Sprintf("b-%d", i), Task: HotspotLLM, Run: sleepJob(&meter, time.Millisecond)}); err != nil {
			t.Fatalf("submit b-%d: %v", i, err)
		}
	}
	results := q2.Close(context.Background())
	if len(results) != 3 {
		t.Errorf("ran %d jobs, want 3 -- a dead ledger must not stop the run", len(results))
	}
	for _, r := range results {
		if r.Err != nil {
			t.Errorf("%s: %v", r.Job.ID, r.Err)
		}
	}
	if atomic.LoadInt32(&reported) == 0 {
		t.Error("OnLedgerError never fired; a run with no checkpoint looks identical to one with")
	}
}

func TestQueueRejectsBadJobs(t *testing.T) {
	q, err := NewQueue(QueueConfig{Workers: 2, Capacity: 4})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	defer q.Close(context.Background())

	if err := q.Submit(context.Background(), Job{ID: "x"}); !errors.Is(err, ErrJobIncomplete) {
		t.Errorf("a job with no Run returned %v, want ErrJobIncomplete", err)
	}
	if err := q.Submit(context.Background(), Job{Run: sleepJob(&concurrencyCounter{}, time.Millisecond)}); err == nil {
		t.Error("a job with no id was accepted")
	}
	// Two jobs, one id: the ledger would record one and the run would do both.
	if err := q.Submit(context.Background(), Job{ID: "dup", Run: sleepJob(&concurrencyCounter{}, time.Millisecond)}); err != nil {
		t.Fatalf("submit dup: %v", err)
	}
	if err := q.Submit(context.Background(), Job{ID: "dup", Run: sleepJob(&concurrencyCounter{}, time.Millisecond)}); !errors.Is(err, ErrDuplicateJob) {
		t.Errorf("a duplicate id returned %v, want ErrDuplicateJob", err)
	}
}

// A submission that lands after Close is refused loudly. Accepting it would mean
// a dub that silently loses its last chunk.
func TestSubmitAfterCloseIsRefused(t *testing.T) {
	q, err := NewQueue(QueueConfig{Workers: 2, Capacity: 4})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	q.Close(context.Background())

	err = q.Submit(context.Background(), Job{ID: "late", Run: sleepJob(&concurrencyCounter{}, time.Millisecond)})
	if !errors.Is(err, ErrQueueClosed) {
		t.Errorf("submit after Close = %v, want ErrQueueClosed", err)
	}
	// Close twice must not panic on a closed channel.
	q.Close(context.Background())
}

func TestResultsComeBackInSubmissionOrder(t *testing.T) {
	q, err := NewQueue(QueueConfig{Workers: 4, Capacity: 16})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	// Reverse the natural completion order: later jobs finish first.
	for i := 0; i < 6; i++ {
		dwell := time.Duration(6-i) * 10 * time.Millisecond
		if err := q.Submit(context.Background(), Job{
			ID:   fmt.Sprintf("job-%d", i),
			Task: HotspotLLM,
			Run:  sleepJob(&concurrencyCounter{}, dwell),
		}); err != nil {
			t.Fatalf("submit: %v", err)
		}
	}
	results := q.Close(context.Background())
	if len(results) != 6 {
		t.Fatalf("got %d results", len(results))
	}
	for i, r := range results {
		if want := fmt.Sprintf("job-%d", i); r.Job.ID != want {
			t.Errorf("result %d is %s, want %s", i, r.Job.ID, want)
		}
		if r.Seq != i {
			t.Errorf("result %d has Seq %d", i, r.Seq)
		}
		if r.Duration() <= 0 {
			t.Errorf("%s has duration %s", r.Job.ID, r.Duration())
		}
	}
}

func TestStatsReportsTheSplit(t *testing.T) {
	q, err := NewQueue(QueueConfig{Workers: 4, Capacity: 8})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	var meter concurrencyCounter
	for i := 0; i < 4; i++ {
		if err := q.Submit(context.Background(), Job{
			ID:   fmt.Sprintf("llm-%d", i),
			Task: HotspotLLM,
			Run:  sleepJob(&meter, 15*time.Millisecond),
		}); err != nil {
			t.Fatalf("submit: %v", err)
		}
	}
	stats := q.Stats()
	if stats.Capacity != 8 || stats.Workers != 4 {
		t.Errorf("stats capacity/workers = %d/%d, want 8/4", stats.Capacity, stats.Workers)
	}
	q.Close(context.Background())

	stats = q.Stats()
	if stats.Completed != 4 || stats.Failed != 0 {
		t.Errorf("stats completed/failed = %d/%d, want 4/0", stats.Completed, stats.Failed)
	}
	if stats.MaxPerHotspot[HotspotLLM] != 1 {
		t.Errorf("MaxPerHotspot[llm] = %d, want 1", stats.MaxPerHotspot[HotspotLLM])
	}
	if stats.Running != 0 {
		t.Errorf("Running = %d after Close, want 0", stats.Running)
	}
}

// A queue that accepts jobs nobody will ever pick up is the worst of the three
// options: the run reports no error and does no work.
func TestFirstSubmitStartsThePool(t *testing.T) {
	q, err := NewQueue(QueueConfig{Workers: 2, Capacity: 4})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	var meter concurrencyCounter
	// Deliberately no Start().
	for i := 0; i < 3; i++ {
		if err := q.Submit(context.Background(), Job{ID: fmt.Sprintf("j-%d", i), Run: sleepJob(&meter, time.Millisecond)}); err != nil {
			t.Fatalf("submit: %v", err)
		}
	}
	results := q.Close(context.Background())
	if len(results) != 3 {
		t.Fatalf("ran %d jobs without calling Start; want 3", len(results))
	}
}

// Two locks is a claim, so it gets checked. The race this is guarding is a
// producer blocked on a full queue while another goroutine calls Close; without
// the ordering, that is "send on closed channel", which panics the whole run
// instead of failing one submission.
func TestCloseRacingABlockedSubmitterDoesNotPanic(t *testing.T) {
	for round := 0; round < 20; round++ {
		q, err := NewQueue(QueueConfig{Workers: 1, Capacity: 1})
		if err != nil {
			t.Fatalf("NewQueue: %v", err)
		}
		release := make(chan struct{})
		var once sync.Once
		unblock := func() { once.Do(func() { close(release) }) }
		defer unblock()

		if err := q.Submit(context.Background(), Job{ID: "blocker", Run: func(context.Context, Job) error {
			<-release
			return nil
		}}); err != nil {
			t.Fatalf("submit blocker: %v", err)
		}

		var wg sync.WaitGroup
		for i := 0; i < 4; i++ {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				// These will block behind the full queue.
				_ = q.Submit(context.Background(), Job{ID: fmt.Sprintf("w-%d", i), Run: sleepJob(&concurrencyCounter{}, time.Millisecond)})
			}(i)
		}
		// Give them a moment to actually reach the blocked send, then close.
		time.Sleep(2 * time.Millisecond)
		closeCtx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
		go func() {
			defer cancel()
			q.Close(closeCtx)
		}()
		time.Sleep(3 * time.Millisecond)
		unblock()
		wg.Wait()
		cancel()
		q.Close(context.Background())
	}
}

// The default capacity must not accidentally serialise the two lanes.
func TestDefaultsGiveOneWorkerPerLane(t *testing.T) {
	q, err := NewQueue(QueueConfig{})
	if err != nil {
		t.Fatalf("NewQueue: %v", err)
	}
	st := q.Stats()
	if st.Workers != 4 {
		t.Errorf("default workers = %d, want 4", st.Workers)
	}
	if st.Capacity != 16 {
		t.Errorf("default capacity = %d, want 16 (workers*4)", st.Capacity)
	}
}
