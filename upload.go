package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gotd/td/telegram/message"
	"github.com/gotd/td/telegram/message/styling"
	"github.com/gotd/td/tg"
)

// uploader is the shared state every worker touches.
type uploader struct {
	pool     *pool
	path     string
	filename string
	channel  string
	caption  string

	// planOut is rewritten as parts land, so an interrupted run stays resumable.
	planOut string

	mu       sync.Mutex
	stored   map[int]StoredPart
	failures map[int]error

	sentBytes atomic.Int64
	started   time.Time
	total     int64
	count     int
	chunk     int64
	limit     int
}

func newUploader(
	p *pool,
	path, filename, channel, caption, planOut string,
	plan Plan,
	started time.Time,
	limit int,
) *uploader {
	if limit < 1 {
		limit = 1
	}
	if limit > maxConcurrency {
		limit = maxConcurrency
	}
	return &uploader{
		pool: p, path: path, filename: filename, channel: channel,
		caption: caption, planOut: planOut,
		stored:   make(map[int]StoredPart, len(plan.Parts)),
		failures: make(map[int]error),
		started:  started,
		total:    plan.TotalSize,
		count:    len(plan.Parts),
		chunk:    plan.ChunkSize,
		limit:    limit,
	}
}

// sendPart streams one part to Telegram over one connection.
//
// The promise callback is gotd/td's documented way to send a file: it receives
// an Uploader, and FromReader streams a byte range. Nothing is buffered whole,
// so a 1.85 GB part costs a few megabytes of memory regardless of its size.
func (u *uploader) sendPart(ctx context.Context, c *conn, p Part) (StoredPart, error) {
	entry := StoredPart{
		Part:   p.Number,
		Offset: p.Offset,
		Size:   p.Size,
		SHA256: p.SHA256,
		Name:   partName(u.filename, p.Number, u.count),
	}

	file, err := os.Open(u.path)
	if err != nil {
		return entry, err
	}
	defer file.Close()
	if _, err := file.Seek(p.Offset, io.SeekStart); err != nil {
		return entry, err
	}

	caption := u.caption
	if caption == "" {
		if u.count == 1 {
			caption = fmt.Sprintf("%s (%s)",
				u.filename, humanBytes(float64(p.Size)))
		} else {
			caption = fmt.Sprintf("Part %d/%d Â· %s\n%s",
				p.Number, u.count, u.filename, humanBytes(float64(p.Size)))
		}
	}

	promise := message.Upload(
		func(ctx context.Context, up message.Uploader) (tg.InputFileClass, error) {
			// LimitReader keeps the stream inside this part's byte range.
			return up.FromReader(ctx, entry.Name, io.LimitReader(file, p.Size))
		})

	updates, err := c.peer.Upload(promise).File(ctx, styling.Plain(caption))
	if err != nil {
		return entry, err
	}

	entry.MessageID = firstMessageID(updates)
	entry.Link = fmt.Sprintf("https://t.me/%s/%d",
		trimAt(u.channel), entry.MessageID)
	return entry, nil
}

// runAll pushes every part, with as many parts in flight as the pool has
// connections.
//
// This is the part that matters for throughput, and the reason the program is
// written this way: each in-flight part sits on its own MTProto connection, so
// each has its own TCP receive window. Three in flight carry roughly three
// windows, which is what it takes to fill a 404 KB pipe over a 110 ms round
// trip. Goroutines on a single client would have changed nothing.
func (u *uploader) runAll(ctx context.Context, plan Plan, skip map[int]bool) {
	pending := make([]Part, 0, len(plan.Parts))
	for _, p := range plan.Parts {
		if skip[p.Number] {
			u.record(StoredPart{
				Part: p.Number, Offset: p.Offset, Size: p.Size,
				SHA256: p.SHA256,
				Name:   partName(u.filename, p.Number, u.count),
			}, false)
			emit(Progress{Event: "part_skipped", Part: p.Number,
				PartCount: u.count,
				Message:   "already stored, not resending"})
			continue
		}
		pending = append(pending, p)
	}
	if len(pending) == 0 {
		return
	}

	queue := make(chan Part)
	var wg sync.WaitGroup

	worker := func() {
		defer wg.Done()
		// One connection per worker. Draining the queue in order means the
		// first `limit` parts start together and the rest follow as each
		// finishes, which keeps the link busy without unbounded memory use.
		for p := range queue {
			if ctx.Err() != nil {
				u.fail(p.Number, ctx.Err())
				continue
			}
			entry, err := u.sendPart(ctx, u.pool.next(), p)
			if err != nil {
				u.fail(p.Number, err)
				emit(Progress{Event: "part_failed", Part: p.Number,
					PartCount: u.count, Message: err.Error()})
				continue
			}
			u.record(entry, true)
		}
	}

	workers := u.limit
	if workers > len(pending) {
		workers = len(pending)
	}
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go worker()
	}
	for _, p := range pending {
		queue <- p
	}
	close(queue)
	wg.Wait()
}

func (u *uploader) fail(number int, err error) {
	u.mu.Lock()
	u.failures[number] = err
	u.mu.Unlock()
}

// record stores a confirmed part and persists the resume plan.
func (u *uploader) record(entry StoredPart, sent bool) {
	u.mu.Lock()
	u.stored[entry.Part] = entry
	confirmed := make([]Part, 0, len(u.stored))
	for _, p := range u.stored {
		confirmed = append(confirmed, Part{
			Number: p.Part, Offset: p.Offset, Size: p.Size, SHA256: p.SHA256,
		})
	}
	u.mu.Unlock()

	// A skipped part was already in Telegram before this run, so counting it
	// against this run's rate would flatter the number.
	if sent {
		u.sentBytes.Add(entry.Size)
	}
	elapsed := time.Since(u.started).Seconds()
	message := fmt.Sprintf("part %d/%d stored (%s)",
		entry.Part, u.count, humanBytes(float64(entry.Size)))
	if !sent {
		message = fmt.Sprintf("part %d/%d already stored", entry.Part, u.count)
	}
	emit(Progress{
		Event:       "part_stored",
		Part:        entry.Part,
		PartCount:   u.count,
		Bytes:       u.sentBytes.Load(),
		Total:       u.total,
		BytesPerSec: float64(u.sentBytes.Load()) / max64(elapsed, 0.001),
		ElapsedSec:  elapsed,
		Message:     message,
	})
	u.savePlan(confirmed)
}

// savePlan rewrites the resume file with whatever is confirmed so far. Doing it
// after each part is what makes a crash cost one part rather than the whole
// file.
func (u *uploader) savePlan(confirmed []Part) {
	if u.planOut == "" {
		return
	}
	sort.Slice(confirmed, func(i, j int) bool {
		return confirmed[i].Number < confirmed[j].Number
	})
	plan := Plan{
		Version:   1,
		ChunkSize: u.chunk,
		TotalSize: u.total,
		Filename:  u.filename,
		CreatedAt: nowStamp(),
		Parts:     confirmed,
	}
	if err := writeJSON(u.planOut, plan); err != nil {
		logf("warning: could not save the resume plan: %v", err)
	}
}

// summary renders the run as the machine-readable result.
func (u *uploader) summary(plan Plan, channel string) Result {
	u.mu.Lock()
	defer u.mu.Unlock()

	ordered := make([]StoredPart, 0, len(u.stored))
	var skipped []int
	for _, p := range plan.Parts {
		entry, ok := u.stored[p.Number]
		if !ok {
			continue
		}
		ordered = append(ordered, entry)
		if entry.MessageID == 0 {
			skipped = append(skipped, p.Number)
		}
	}
	elapsed := time.Since(u.started).Seconds()
	return Result{
		OK:           len(u.failures) == 0 && len(ordered) == len(plan.Parts),
		Channel:      channel,
		Filename:     plan.Filename,
		TotalSize:    plan.TotalSize,
		Chunked:      len(plan.Parts) > 1,
		ChunkCount:   len(plan.Parts),
		Concurrency:  u.limit,
		Parts:        ordered,
		SkippedParts: skipped,
		ElapsedSec:   elapsed,
		BytesPerSec:  float64(u.sentBytes.Load()) / max64(elapsed, 0.001),
	}
}

func (u *uploader) firstFailure() error {
	u.mu.Lock()
	defer u.mu.Unlock()
	smallest := 0
	var first error
	for number, err := range u.failures {
		if first == nil || number < smallest {
			smallest = number
			first = err
		}
	}
	if first == nil {
		return errors.New("unknown failure")
	}
	return first
}

// firstMessageID digs the new message id out of what Telegram returns.
//
// A message sent to a channel comes back as Updates carrying an
// UpdateNewChannelMessage; sent to a chat it comes back as
// UpdateShortSentMessage, which carries the id directly.
func firstMessageID(updates tg.UpdatesClass) int {
	if updates == nil {
		return 0
	}
	fromUpdate := func(item tg.UpdateClass) int {
		switch u := item.(type) {
		case *tg.UpdateNewMessage:
			if m, ok := u.Message.(*tg.Message); ok {
				return m.ID
			}
		case *tg.UpdateNewChannelMessage:
			if m, ok := u.Message.(*tg.Message); ok {
				return m.ID
			}
		}
		return 0
	}
	fromList := func(items []tg.UpdateClass) int {
		for _, item := range items {
			if id := fromUpdate(item); id != 0 {
				return id
			}
		}
		return 0
	}

	switch v := updates.(type) {
	case *tg.UpdateShortSentMessage:
		return v.ID
	case *tg.UpdateShort:
		// UpdateShort wraps a single Update, not a list.
		return fromUpdate(v.Update)
	case *tg.Updates:
		return fromList(v.Updates)
	case *tg.UpdatesCombined:
		return fromList(v.Updates)
	}
	return 0
}

// postManifest uploads the manifest as the album's final message. This is the
// message a restore looks for, so an archive made here is restorable by the
// existing Python tool with no changes.
func postManifest(
	ctx context.Context,
	c *conn,
	channel, filename string,
	manifest Manifest,
) (string, int, error) {
	data, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return "", 0, err
	}
	name := filename + ".manifest.json"

	promise := message.Upload(
		func(ctx context.Context, up message.Uploader) (tg.InputFileClass, error) {
			return up.FromBytes(ctx, name, data)
		})
	caption := fmt.Sprintf(
		"MANIFEST Â· %s\n%d part(s) Â· %s\n\nPaste this link into T_Dubber â†’ Telegram Drive â†’ Restore.",
		filename, manifest.ChunkCount, humanBytes(float64(manifest.Size)))

	updates, err := c.peer.Upload(promise).File(ctx, styling.Plain(caption))
	if err != nil {
		return "", 0, err
	}
	id := firstMessageID(updates)
	return fmt.Sprintf("https://t.me/%s/%d", trimAt(channel), id), id, nil
}
