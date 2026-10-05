package main

import (
	"bufio"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gotd/td/telegram/downloader"
	"github.com/gotd/td/telegram/message"
	"github.com/gotd/td/telegram/message/peer"
	"github.com/gotd/td/telegram/message/styling"
	"github.com/gotd/td/telegram/query"
	"github.com/gotd/td/tg"
)

// downloadOne streams a document to disk and confirms it actually landed.
//
// Telegram keeps deleted media as a zero-byte placeholder, so a size check is
// the difference between "restored" and "restored an empty file".
func downloadOne(
	ctx context.Context,
	raw *tg.Client,
	doc *tg.Document,
	dest string,
	threads int,
	verifyHashes bool,
) error {
	if doc == nil {
		return errors.New("that message has no file attached")
	}
	location := &tg.InputDocumentFileLocation{
		ID:            doc.ID,
		AccessHash:    doc.AccessHash,
		FileReference: doc.FileReference,
	}
	_, err := downloader.NewDownloader().
		Download(raw, location).
		WithThreads(threads).
		WithVerify(verifyHashes).
		ToPath(ctx, dest)
	if err != nil {
		return err
	}
	info, err := os.Stat(dest)
	if err != nil {
		return err
	}
	if info.Size() == 0 {
		_ = os.Remove(dest)
		return errors.New("Telegram returned an empty file; the media may have expired")
	}
	return nil
}

// tgReference is a parsed message link: which chat, which message.
type tgReference struct {
	// Username is set for public links. Otherwise Channel carries the numeric
	// id that a t.me/c/... link holds.
	Username  string
	Channel   int64
	Public    bool
	MessageID int
}

// parseTGLink accepts the forms a person actually pastes: t.me/name/123,
// t.me/c/1234567890/12, name/123, or a tg:// deep link.
func parseTGLink(raw string) (tgReference, error) {
	text := strings.TrimSpace(raw)
	if text == "" {
		return tgReference{}, errors.New("empty link")
	}

	if strings.HasPrefix(text, "tg://") {
		values := map[string]string{}
		for _, pair := range strings.Split(text[len("tg://"):], "&") {
			if bits := strings.SplitN(pair, "=", 2); len(bits) == 2 {
				values[bits[0]] = bits[1]
			}
		}
		id, err := strconv.Atoi(values["message_id"])
		if err != nil {
			return tgReference{}, fmt.Errorf("deep link has no usable message id: %s", raw)
		}
		if channel := values["channel"]; channel != "" {
			if n, err := strconv.ParseInt(channel, 10, 64); err == nil {
				return tgReference{Channel: n, MessageID: id}, nil
			}
		}
		username := values["user_id"]
		if username == "" {
			username = values["domain"]
		}
		return tgReference{Username: username, Public: true, MessageID: id}, nil
	}

	text = strings.TrimPrefix(strings.TrimPrefix(text, "https://"), "http://")
	if idx := strings.IndexAny(text, "?#"); idx >= 0 {
		text = text[:idx]
	}
	parts := strings.Split(strings.Trim(text, "/"), "/")
	if len(parts) < 2 {
		return tgReference{}, fmt.Errorf(
			"%q names a chat, not a message; open the file and copy its message link", raw)
	}

	// t.me/c/<channel-id>/<message-id> is a private chat link. Telegram strips
	// the -100 prefix when writing it, so it must not be added back here.
	if parts[0] == "c" && len(parts) >= 3 {
		channel, err := strconv.ParseInt(parts[1], 10, 64)
		if err != nil {
			return tgReference{}, fmt.Errorf("bad channel id in %q", raw)
		}
		id, err := strconv.Atoi(parts[2])
		if err != nil {
			return tgReference{}, fmt.Errorf("bad message id in %q", raw)
		}
		return tgReference{Channel: channel, MessageID: id}, nil
	}

	id, err := strconv.Atoi(parts[1])
	if err != nil {
		return tgReference{}, fmt.Errorf("bad message id in %q", raw)
	}
	return tgReference{Username: parts[0], Public: true, MessageID: id}, nil
}

// peerFor turns a reference into something the API accepts.
func peerFor(ctx context.Context, raw *tg.Client, ref tgReference) (tg.InputPeerClass, error) {
	if !ref.Public {
		return &tg.InputPeerChannel{ChannelID: ref.Channel}, nil
	}
	// ResolveDomain returns a Promise, so it is invoked with the context here.
	resolved, err := peer.ResolveDomain(peer.Plain(raw), ref.Username)(ctx)
	if err != nil {
		return nil, fmt.Errorf("cannot resolve %s: %w", ref.Username, err)
	}
	return resolved, nil
}

// documentOf pulls the attached document out of a message.
//
// A message carries media as a union, not as a field, so a typed switch is the
// only way to reach the file. Service messages and media-free posts are not
// errors here: the caller reports something better than "no document".
func documentOf(msg *tg.Message) (*tg.Document, bool) {
	if msg == nil {
		return nil, false
	}
	media, ok := msg.Media.(*tg.MessageMediaDocument)
	if !ok {
		return nil, false
	}
	doc, ok := media.Document.(*tg.Document)
	return doc, ok
}

func documentFilename(doc *tg.Document) string {
	if doc == nil {
		return ""
	}
	for _, attr := range doc.Attributes {
		if named, ok := attr.(*tg.DocumentAttributeFilename); ok {
			return named.FileName
		}
	}
	return ""
}

// fetchMessage resolves a link and returns that exact message.
//
// GetMessages with an explicit id is the right call, not a history walk:
// history anchored at 0 returns the newest message in the chat, which would
// silently download whatever was posted last instead of the file asked for.
func fetchMessage(ctx context.Context, raw *tg.Client, ref tgReference) (*tg.Message, error) {
	target, err := peerFor(ctx, raw, ref)
	if err != nil {
		return nil, err
	}
	classes, err := query.Messages(raw).GetMessages(ctx, target, ref.MessageID)
	if err != nil {
		return nil, err
	}
	if len(classes) == 0 {
		return nil, fmt.Errorf(
			"message %d is not reachable; it may be deleted or you may not be a member",
			ref.MessageID)
	}
	msg, ok := classes[0].(*tg.Message)
	if !ok {
		return nil, fmt.Errorf("message %d is not a file", ref.MessageID)
	}
	return msg, nil
}

// ---------------------------------------------------------------------------
// fetch
// ---------------------------------------------------------------------------

// cmdFetch pulls an archive back down and verifies it.
//
// Concurrency matters here for the same reason it does on upload: a single
// stream downloads at the same throttled rate it uploads at, because the
// bottleneck is the round trip, not the disk.
func cmdFetch(args []string) int {
	fs := flag.NewFlagSet("fetch", flag.ExitOnError)
	creds := addCredentialFlags(fs)
	link := fs.String("link", "", "manifest or part link")
	dest := fs.String("dest", ".", "directory to write into")
	concurrency := fs.Int("concurrency", 4, "parts downloaded at once")
	resultOut := fs.String("result-out", "", "write the result here")
	verify := fs.Bool("verify", true, "check every part's SHA-256")
	session := addSessionFlag(fs)
	_ = fs.Parse(args)
	applySessionFlag(*session)

	if *link == "" {
		fmt.Fprintln(os.Stderr, "error: --link is required")
		return 2
	}
	var cred credentials
	if err := creds.resolve(&cred); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		return 2
	}
	if err := os.MkdirAll(*dest, 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		return 2
	}

	ctx, cancel := signalContext()
	defer cancel()

	in := bufio.NewReader(os.Stdin)
	started := time.Now()
	result := Result{Mode: "fetch", Concurrency: *concurrency}

	// Downloads go through the same pool as uploads: one connection per part in
	// flight, which is what stops a restore from being as slow as the upload was.
	p, err := openPool(ctx, cred, firstChannel(*link), *concurrency, in)
	if err != nil {
		result.OK = false
		result.Error = err.Error()
		result.ElapsedSec = time.Since(started).Seconds()
		writeResult(*resultOut, result)
		fmt.Fprintf(os.Stderr, "fetch failed: %v\n", err)
		return 1
	}
	defer p.close()

	runErr := func() error {
		raw := tg.NewClient(p.conns[0].client)

		ref, err := parseTGLink(*link)
		if err != nil {
			return err
		}
		msg, err := fetchMessage(ctx, raw, ref)
		if err != nil {
			return err
		}
		doc, ok := documentOf(msg)
		if !ok {
			return errors.New("that message has no file attached")
		}

		// Our manifest is the .json attachment; anything else is a bare part.
		if strings.HasSuffix(strings.ToLower(documentFilename(doc)), ".json") {
			scratch := filepath.Join(*dest, "tgup_manifest.json")
			if err := downloadOne(ctx, raw, doc, scratch, *concurrency, false); err != nil {
				return err
			}
			defer os.Remove(scratch)

			data, err := os.ReadFile(scratch)
			if err != nil {
				return err
			}
			var manifest Manifest
			if err := jsonUnmarshal(data, &manifest); err != nil {
				return fmt.Errorf("that JSON is not a tgup manifest: %w", err)
			}
			if manifest.Marker != manifestKey {
				return fmt.Errorf("unrecognised manifest marker %q; expected %q",
					manifest.Marker, manifestKey)
			}
			return fetchArchive(ctx, p, manifest, *dest, *concurrency, *verify,
				&result, started)
		}

		// A single part: deliver it as it is, there is nothing to reassemble.
		target := filepath.Join(*dest, sanitise(documentFilename(doc)))
		if err := downloadOne(ctx, raw, doc, target, *concurrency, true); err != nil {
			return err
		}
		if info, err := os.Stat(target); err == nil {
			result.TotalSize = info.Size()
			result.VerifiedBytes = info.Size()
			result.VerifiedOK = true
		}
		result.Filename = documentFilename(doc)
		result.OK = true
		logf("single-file archive written to %s", target)
		return nil
	}()

	result.ElapsedSec = time.Since(started).Seconds()
	if result.ElapsedSec > 0 && result.TotalSize > 0 {
		result.BytesPerSec = float64(result.TotalSize) / result.ElapsedSec
	}
	if runErr != nil {
		result.OK = false
		result.Error = runErr.Error()
	}
	writeResult(*resultOut, result)

	fmt.Println()
	fmt.Printf("elapsed     %.1fs\n", result.ElapsedSec)
	fmt.Printf("rate        %s/s\n", humanBytes(result.BytesPerSec))
	if result.OK {
		if result.VerifiedOK {
			fmt.Println("verified    every part matched its checksum")
		} else {
			fmt.Println("written     verification was skipped, so trust is unproven")
		}
	}
	if runErr != nil {
		fmt.Fprintf(os.Stderr, "fetch failed: %v\n", runErr)
		return 1
	}
	return 0
}

// firstChannel reports whatever chat the link names, purely so the connection
// pool has a peer to resolve. Fetch addresses each part by its own link, so a
// private t.me/c/... link simply yields the empty string and the pool resolves
// lazily per part instead.
func firstChannel(link string) string {
	ref, err := parseTGLink(link)
	if err != nil || !ref.Public {
		return ""
	}
	return "@" + ref.Username
}

// fetchArchive downloads every part concurrently, verifies it, and only then
// concatenates.
func fetchArchive(
	ctx context.Context,
	p *pool,
	manifest Manifest,
	dest string,
	concurrency int,
	verify bool,
	result *Result,
	started time.Time,
) error {
	total := len(manifest.Parts)
	if total == 0 {
		return errors.New("the manifest lists no parts")
	}
	if concurrency < 1 {
		concurrency = 1
	}
	if concurrency > maxConcurrency {
		concurrency = maxConcurrency
	}

	scratch, err := os.MkdirTemp(dest, "tgup_parts_")
	if err != nil {
		return err
	}
	defer os.RemoveAll(scratch)

	type outcome struct {
		index int
		part  StoredPart
		size  int64
		err   error
	}
	outcomes := make(chan outcome, total)
	queue := make(chan StoredPart, total)

	var downloaded atomic.Int64
	stop := make(chan struct{})
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-stop:
				return
			case <-ticker.C:
				seen := downloaded.Load()
				elapsed := time.Since(started).Seconds()
				emit(Progress{
					Event:       "download_progress",
					Bytes:       seen,
					Total:       manifest.Size,
					BytesPerSec: float64(seen) / max64(elapsed, 0.001),
					ElapsedSec:  elapsed,
					Message: fmt.Sprintf("%s of %s",
						humanBytes(float64(seen)),
						humanBytes(float64(manifest.Size))),
				})
			}
		}
	}()
	defer close(stop)

	for _, part := range manifest.Parts {
		queue <- part
	}
	close(queue)

	var wg sync.WaitGroup
	worker := func() {
		defer wg.Done()
		for part := range queue {
			if ctx.Err() != nil {
				outcomes <- outcome{index: part.Part, part: part, err: ctx.Err()}
				continue
			}
			size, err := fetchOnePart(ctx, p, part, scratch, verify, &downloaded,
				manifest.Size, total)
			outcomes <- outcome{index: part.Part, part: part, size: size, err: err}
		}
	}
	workers := concurrency
	if workers > total {
		workers = total
	}
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go worker()
	}
	wg.Wait()
	close(outcomes)

	collected := make(map[int]outcome)
	var firstErr error
	failed := 0
	for out := range outcomes {
		if out.err != nil {
			failed++
			if firstErr == nil {
				firstErr = out.err
			}
			continue
		}
		collected[out.index] = out
	}
	if firstErr != nil {
		// Report what did land, so a resume knows where to pick up.
		return fmt.Errorf("%d of %d parts could not be restored: %w",
			failed, total, firstErr)
	}

	indices := make([]int, 0, len(collected))
	for index := range collected {
		indices = append(indices, index)
	}
	sort.Ints(indices)

	// Assemble under a staging name and rename only once the byte count agrees
	// with the manifest. A half-written restore must never be mistaken for a
	// finished one.
	staging := filepath.Join(dest, manifest.Filename+".restoring")
	final := filepath.Join(dest, sanitise(manifest.Filename))
	_ = os.Remove(staging)

	out, err := os.Create(staging)
	if err != nil {
		return err
	}
	var assembled int64
	for _, index := range indices {
		partPath := filepath.Join(scratch, fmt.Sprintf("part%05d", index))
		in, err := os.Open(partPath)
		if err != nil {
			out.Close()
			os.Remove(staging)
			return err
		}
		written, err := io.Copy(out, in)
		in.Close()
		if err != nil {
			out.Close()
			os.Remove(staging)
			return err
		}
		assembled += written
	}
	if err := out.Close(); err != nil {
		os.Remove(staging)
		return err
	}

	if manifest.Size > 0 && assembled != manifest.Size {
		os.Remove(staging)
		return fmt.Errorf(
			"rebuilt %s but the manifest declares %s; nothing was kept",
			humanBytes(float64(assembled)), humanBytes(float64(manifest.Size)))
	}
	if err := os.Rename(staging, final); err != nil {
		os.Remove(staging)
		return err
	}

	result.OK = true
	result.Filename = manifest.Filename
	result.TotalSize = assembled
	result.VerifiedBytes = assembled
	result.VerifiedOK = verify
	result.Chunked = manifest.Chunked
	result.ChunkCount = total
	result.SourceSHA256 = manifest.SourceSHA256
	for _, index := range indices {
		result.Parts = append(result.Parts, collected[index].part)
	}
	logf("rebuilt %s from %d verified parts -> %s",
		humanBytes(float64(assembled)), total, final)
	return nil
}

// fetchOnePart downloads a single part onto the scratch disk and verifies it.
//
// Verification happens before the part is accepted, not after assembly: a part
// that arrived corrupted must never be joined into the file.
func fetchOnePart(
	ctx context.Context,
	p *pool,
	part StoredPart,
	scratch string,
	verify bool,
	downloaded *atomic.Int64,
	total int64,
	partCount int,
) (int64, error) {
	ref, err := parseTGLink(part.Link)
	if err != nil {
		return 0, err
	}
	c := p.next()
	raw := tg.NewClient(c.client)

	msg, err := fetchMessage(ctx, raw, ref)
	if err != nil {
		return 0, err
	}
	doc, ok := documentOf(msg)
	if !ok {
		return 0, fmt.Errorf("part %d has no file attached", part.Part)
	}

	partPath := filepath.Join(scratch, fmt.Sprintf("part%05d", part.Part))
	if err := downloadOne(ctx, raw, doc, partPath, 1, false); err != nil {
		return 0, err
	}
	var size int64
	if info, err := os.Stat(partPath); err == nil {
		size = info.Size()
	}

	if verify && part.SHA256 != "" {
		digest, err := hashWhole(partPath)
		if err != nil {
			return 0, err
		}
		if digest != part.SHA256 {
			return 0, fmt.Errorf(
				"part %d failed verification: expected %s…, got %s…",
				part.Part, part.SHA256[:16], digest[:16])
		}
	}
	downloaded.Add(size)
	emit(Progress{Event: "part_downloaded", Part: part.Part,
		PartCount: partCount, Bytes: downloaded.Load(), Total: total,
		Message: fmt.Sprintf("part %d/%d verified (%s)",
			part.Part, partCount, humanBytes(float64(size)))})
	return size, nil
}

// ---------------------------------------------------------------------------
// bench
// ---------------------------------------------------------------------------

// cmdBench measures what the link actually delivers.
//
// This exists because "run it concurrently, it goes faster" is a claim, not a
// fact. It sends the same payload over one connection, then over several, and
// prints the rate for each level, so the answer comes from the network rather
// than from arithmetic about bandwidth-delay products.
func cmdBench(args []string) int {
	fs := flag.NewFlagSet("bench", flag.ExitOnError)
	creds := addCredentialFlags(fs)
	channel := fs.String("channel", "", "scratch channel to measure against")
	size := fs.Int64("size", 48<<20, "payload bytes per round")
	levels := fs.String("concurrency", "1,2,3,4",
		"comma-separated concurrency levels to try")
	resultOut := fs.String("result-out", "", "write the result here")
	session := addSessionFlag(fs)
	_ = fs.Parse(args)
	applySessionFlag(*session)

	var cred credentials
	if err := creds.resolve(&cred); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		return 2
	}
	if *channel == "" {
		fmt.Fprintln(os.Stderr, "error: --channel is required")
		return 2
	}

	steps := []int{}
	for _, token := range strings.Split(*levels, ",") {
		if n, err := strconv.Atoi(strings.TrimSpace(token)); err == nil && n > 0 {
			steps = append(steps, n)
		}
	}
	if len(steps) == 0 {
		steps = []int{1, 2, 3}
	}

	// One payload, reused every round, so disk read speed is not the variable.
	payload, payloadSize, err := makeBenchPayload(*size)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		return 2
	}
	defer os.Remove(payload)

	ctx, cancel := signalContext()
	defer cancel()
	in := bufio.NewReader(os.Stdin)

	type row struct {
		Concurrency int     `json:"concurrency"`
		Seconds     float64 `json:"seconds"`
		BytesPerSec float64 `json:"bytes_per_sec"`
	}
	rows := []row{}
	summary := map[string]any{
		"mode":    "bench",
		"rows":    rows,
		"payload": payloadSize,
		"channel": *channel,
	}

	// Open the widest pool once and narrow it per round. Logging in repeatedly
	// would dominate a 48 MB measurement.
	widest := steps[len(steps)-1]
	p, err := openPool(ctx, cred, trimAt(*channel), widest, in)
	if err != nil {
		summary["error"] = err.Error()
		_ = writeJSON(*resultOut, summary)
		fmt.Fprintf(os.Stderr, "bench failed: %v\n", err)
		return 1
	}
	defer p.close()

	fmt.Fprintf(os.Stderr,
		"payload %s per round, %d level(s), widest pool %d connection(s)\n\n",
		humanBytes(float64(payloadSize)), len(steps), len(p.conns))

	for _, level := range steps {
		if level > len(p.conns) {
			level = len(p.conns)
		}
		// A round is one payload split into `level` equal parts sent together.
		// Comparing rounds isolates concurrency as the only thing that changed.
		chunk := payloadSize / int64(level)
		const minChunk = int64(512 * 1024)
		if chunk < minChunk {
			chunk = minChunk
		}
		sent := chunk * int64(level)

		started := time.Now()
		runErr := uploadRound(ctx, p.conns[:level], payload, chunk, level, sent)
		elapsed := time.Since(started).Seconds()
		if runErr != nil {
			fmt.Fprintf(os.Stderr, "  %d connection(s)  FAILED: %v\n", level, runErr)
			continue
		}
		rate := float64(sent) / max64(elapsed, 0.001)
		rows = append(rows, row{Concurrency: level,
			Seconds: elapsed, BytesPerSec: rate})
		fmt.Fprintf(os.Stderr, "  %d connection(s)  %7.1fs  %s/s\n",
			level, elapsed, humanBytes(rate))
	}

	summary["rows"] = rows
	if len(rows) > 0 {
		best := rows[0]
		for _, candidate := range rows[1:] {
			if candidate.BytesPerSec > best.BytesPerSec {
				best = candidate
			}
		}
		summary["best_mb"] = best.BytesPerSec / (1 << 20)
		summary["best_conc"] = best.Concurrency
		fmt.Println()
		fmt.Printf("best        %s/s over %d connection(s)\n",
			humanBytes(best.BytesPerSec), best.Concurrency)
		fmt.Println()
		fmt.Println("Use the lowest level that already reaches the best rate. More")
		fmt.Println("sockets than that only raise the odds of a FloodWait.")
	}
	if *resultOut != "" {
		_ = writeJSON(*resultOut, summary)
	}
	return 0
}

// makeBenchPayload writes a deterministic file so repeat rounds are comparable.
func makeBenchPayload(size int64) (string, int64, error) {
	f, err := os.CreateTemp("", "tgup_bench_*.bin")
	if err != nil {
		return "", 0, err
	}
	defer f.Close()

	block := make([]byte, 1<<20)
	for i := range block {
		block[i] = byte(i * 31)
	}
	for written := int64(0); written < size; written += int64(len(block)) {
		if _, err := f.Write(block); err != nil {
			os.Remove(f.Name())
			return "", 0, err
		}
	}
	return f.Name(), size, nil
}

// uploadRound sends one payload as `parts` equal ranges, one part per
// connection.
func uploadRound(
	ctx context.Context,
	conns []*conn,
	path string,
	chunk int64,
	parts int,
	sent int64,
) error {
	if len(conns) == 0 {
		return errors.New("no connections available")
	}
	type job struct {
		index  int
		offset int64
		size   int64
	}
	jobs := make(chan job, parts)
	for i := 0; i < parts; i++ {
		jobs <- job{index: i + 1, offset: int64(i) * chunk, size: chunk}
	}
	close(jobs)

	errs := make(chan error, parts)
	var wg sync.WaitGroup
	for i := 0; i < parts; i++ {
		c := conns[i%len(conns)]
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := range jobs {
				if err := sendRange(ctx, c, path, j.offset, j.size,
					j.index, parts, sent); err != nil {
					errs <- err
					return
				}
			}
		}()
	}
	wg.Wait()
	close(errs)

	var first error
	for err := range errs {
		if first == nil {
			first = err
		}
	}
	return first
}

// sendRange streams one byte range as a throwaway message.
func sendRange(
	ctx context.Context,
	c *conn,
	path string,
	offset, size int64,
	index, total int,
	sent int64,
) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return err
	}

	name := fmt.Sprintf("tgup_bench.part%03dof%03d.bin", index, total)
	promise := message.Upload(
		func(ctx context.Context, up message.Uploader) (tg.InputFileClass, error) {
			return up.FromReader(ctx, name, io.LimitReader(f, size))
		})
	_, err = c.peer.Upload(promise).File(ctx, styling.Plain(fmt.Sprintf(
		"tgup benchmark %d/%d (%d bytes)", index, total, sent)))
	return err
}
