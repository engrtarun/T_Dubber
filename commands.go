package main

import (
	"bufio"
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type credentialFlags struct {
	apiID      *int
	apiHash    *string
	phone      *string
	stdinCreds *bool
}

func addCredentialFlags(fs *flag.FlagSet) credentialFlags {
	return credentialFlags{
		apiID:      fs.Int("api-id", 0, "Telegram API id"),
		apiHash:    fs.String("api-hash", "", "Telegram API hash"),
		phone:      fs.String("phone", "", "phone with country code; first run only"),
		stdinCreds: fs.Bool("credentials-stdin", false, "Read api_id and api_hash from stdin as JSON"),
	}
}

func (c credentialFlags) resolve(out *credentials) error {
	if c.stdinCreds != nil && *c.stdinCreds {
		reader := bufio.NewReader(os.Stdin)
		line, err := reader.ReadString('\n')
		if err != nil {
			return fmt.Errorf("failed to read credentials from stdin: %v", err)
		}
		var creds struct {
			APIID   int    `json:"api_id"`
			APIHash string `json:"api_hash"`
		}
		if err := jsonUnmarshal([]byte(line), &creds); err != nil {
			return fmt.Errorf("failed to parse JSON credentials: %v", err)
		}
		out.apiID = creds.APIID
		out.apiHash = creds.APIHash
	} else {
		if c.apiID == nil || *c.apiID == 0 {
			return errors.New("--api-id is required or use --credentials-stdin")
		}
		if c.apiHash == nil || *c.apiHash == "" {
			return errors.New("--api-hash is required or use --credentials-stdin")
		}
		out.apiID = *c.apiID
		out.apiHash = *c.apiHash
	}
	out.phone = ""
	if c.phone != nil {
		out.phone = strings.TrimSpace(*c.phone)
	}
	return nil
}

// addSessionFlag registers --session on the subcommands that talk to Telegram.
// `plan` deliberately does not get one: it never opens a connection, so a
// session flag there would only suggest it does.
func addSessionFlag(fs *flag.FlagSet) *string {
	return fs.String("session", "",
		"path to tgup's session file (default: $TGUP_SESSION, else ./tgup.session)")
}

// applySessionFlag points this run's session at the caller's answer. An
// explicit --session wins, then TGUP_SESSION, then the working-directory
// default -- so a worker can export TGUP_SESSION=/kaggle/working/tgup.session
// once and have every invocation reuse the same authorized session instead of
// standing somewhere new and trying to log in again.
func applySessionFlag(value string) {
	if v := strings.TrimSpace(value); v != "" {
		sessionFile = v
		return
	}
	if v := strings.TrimSpace(os.Getenv("TGUP_SESSION")); v != "" {
		sessionFile = v
	}
}

// ---------------------------------------------------------------------------
// plan
// ---------------------------------------------------------------------------

// cmdPlan splits a file and hashes each part. Fully offline: no credentials, no
// session, no network. Useful on its own, and the layout it prints is exactly
// what upload will do.
//
// With --url it plans an http(s) payload instead, which needs no credentials
// either but does need the origin to serve byte ranges. That combination is the
// cheapest possible proof that a link is uploadable: size, part layout and all
// digests, without sending a byte to Telegram and without a session.
func cmdPlan(args []string) int {
	fs := flag.NewFlagSet("plan", flag.ExitOnError)
	file := fs.String("file", "", "file to plan")
	rawURL := fs.String("url", "", "http(s) URL to plan; nothing is written to disk")
	timeout := fs.Duration("url-timeout", 30*time.Second, "how long to wait on the origin")
	chunk := byteSizes(DefaultChunkSize)
	fs.Var(&chunk, "chunk-size", "bytes per part (1900MiB, 512KB, 1048576 ...)")
	out := fs.String("plan-out", "", "write the plan here")
	withHash := fs.Bool("hash", true, "compute each part's SHA-256")
	_ = fs.Parse(args)

	if (*file == "") == (*rawURL == "") {
		fmt.Fprintln(os.Stderr, "error: give exactly one of --file or --url")
		return 2
	}

	ctx, cancel := signalContext()
	defer cancel()

	var src source
	var err error
	if *rawURL != "" {
		src, err = newHTTPSource(ctx, *rawURL, *timeout)
	} else {
		src, err = openFileSource(*file)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		return 2
	}
	defer src.Close()

	plan := buildPlan(src.Name(), src.Describe(), src.Size(), int64(chunk))
	fmt.Printf("source      %s (%s)\n", plan.Filename,
		humanBytes(float64(plan.TotalSize)))
	if *rawURL != "" {
		fmt.Printf("origin      %s\n", src.Describe())
	}
	fmt.Printf("parts       %d of at most %s\n", len(plan.Parts),
		humanBytes(float64(plan.ChunkSize)))
	// Sockets in flight is capped independently of part count: past a point
	// extra connections stop helping and only invite a rate limit.
	inFlight := len(plan.Parts)
	if inFlight > maxConcurrency {
		inFlight = maxConcurrency
	}
	fmt.Printf("concurrency %d connection(s) would cover %d part(s)\n",
		inFlight, len(plan.Parts))

	if *withHash {
		started := time.Now()
		if _, err := hashSource(src, &plan); err != nil {
			fmt.Fprintf(os.Stderr, "error hashing: %v\n", err)
			return 2
		}
		for i := range plan.Parts {
			p := &plan.Parts[i]
			fmt.Printf("  part %d/%d  %s  %s\n", p.Number, len(plan.Parts),
				humanBytes(float64(p.Size)), p.SHA256[:16])
		}
		elapsed := time.Since(started).Seconds()
		fmt.Printf("hashed      %.1fs (%s/s)\n", elapsed,
			humanBytes(float64(plan.TotalSize)/max64(elapsed, 0.001)))
	}

	if *out != "" {
		if err := writeJSON(*out, plan); err != nil {
			fmt.Fprintf(os.Stderr, "error writing plan: %v\n", err)
			return 1
		}
		fmt.Printf("plan        %s\n", *out)
	}
	return 0
}

// ---------------------------------------------------------------------------
// upload
// ---------------------------------------------------------------------------

// byteSizes lets --chunk-size be written the way a human thinks about bytes
// ("1900MiB", "512KB", "64MB") instead of only as a bare integer. It exists
// because the first person to try `--chunk-size 1MB` against the raw Int64 flag
// got a usage dump instead of a split -- and a flag that only accepts the
// default's units is a flag nobody tunes.
type byteSizes int64

func (b *byteSizes) String() string { return strconv.FormatInt(int64(*b), 10) }

func (b *byteSizes) Set(v string) error {
	trimmed := strings.TrimSpace(v)
	if trimmed == "" {
		return errors.New("empty size")
	}
	multiplier := int64(1)
	lower := strings.ToLower(trimmed)
	for _, unit := range []struct {
		suffix string
		mult   int64
	}{
		{"kib", 1 << 10}, {"mib", 1 << 20}, {"gib", 1 << 30}, {"tib", 1 << 40},
		{"kb", 1000}, {"mb", 1000 * 1000}, {"gb", 1000 * 1000 * 1000},
		{"k", 1 << 10}, {"m", 1 << 20}, {"g", 1 << 30},
		{"b", 1},
	} {
		if strings.HasSuffix(lower, unit.suffix) {
			multiplier = unit.mult
			trimmed = trimmed[:len(trimmed)-len(unit.suffix)]
			break
		}
	}
	n, err := strconv.ParseFloat(strings.TrimSpace(trimmed), 64)
	if err != nil {
		return fmt.Errorf("size %q is not a number with an optional unit "+
			"(KB, MiB, GB, ...)", v)
	}
	if n <= 0 {
		return fmt.Errorf("size %q must be greater than zero", v)
	}
	*b = byteSizes(int64(n * float64(multiplier)))
	return nil
}

// cmdUpload is the workhorse: it sends a file's parts over several connections.
func cmdUpload(args []string) int {
	fs := flag.NewFlagSet("upload", flag.ExitOnError)
	creds := addCredentialFlags(fs)
	channel := fs.String("channel", "", "target channel, e.g. @name")
	file := fs.String("file", "", "file to upload")
	rawURL := fs.String("url", "",
		"http(s) URL to upload; streamed part by part, nothing written to disk. "+
			"Exactly one of --file or --url is required")
	urlTimeout := fs.Duration("url-timeout", 30*time.Second,
		"how long to wait on the origin for each request")
	chunk := byteSizes(DefaultChunkSize)
	fs.Var(&chunk, "chunk-size", "bytes per part (1900MiB, 512KB, 1048576 ...)")
	concurrency := fs.Int("concurrency", 3,
		"connections in flight; each one carries its own TCP window")
	planIn := fs.String("plan-in", "", "resume from this plan file")
	planOut := fs.String("plan-out", "", "rewrite the plan after each stored part")
	resultOut := fs.String("result-out", "", "write the machine-readable result here")
	thumbnail := fs.String("thumbnail", "", "cover art (recorded in the manifest; "+
		"Telegram only renders inline artwork on small files, and attaching it to "+
		"a multi-gigabyte part makes clients try to preview the movie instead)")
	caption := fs.String("caption", "", "caption for the upload")
	dryRun := fs.Bool("dry-run", false, "plan and hash only; send nothing")
	session := addSessionFlag(fs)
	_ = fs.Parse(args)
	applySessionFlag(*session)

	if (*file == "") == (*rawURL == "") {
		fmt.Fprintln(os.Stderr,
			"error: give exactly one of --file or --url (both means an ambiguous source, "+
				"neither means nothing to upload)")
		return 2
	}
	if *concurrency < 1 {
		fmt.Fprintln(os.Stderr, "error: --concurrency must be at least 1")
		return 2
	}

	var cred credentials
	if err := creds.resolve(&cred); err != nil && !*dryRun {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		return 2
	}

	ctx, cancel := signalContext()
	defer cancel()

	var src source
	var err error
	if *rawURL != "" {
		src, err = newHTTPSource(ctx, *rawURL, *urlTimeout)
	} else {
		src, err = openFileSource(*file)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		return 2
	}
	defer src.Close()
	logf("source %s (%s)", src.Describe(), humanBytes(float64(src.Size())))

	// Resume: a plan whose parts already carry a digest has been stored before.
	plan := Plan{}
	skip := map[int]bool{}
	if *planIn != "" {
		loaded, err := readPlan(*planIn)
		switch {
		case err != nil:
			logf("resume: no usable plan (%v); starting fresh", err)
		case len(loaded.Parts) == 0:
			logf("resume: the plan lists no parts; starting fresh")
		case !sameSource(loaded, src, loaded.Source):
			// The offsets in the plan are trusted to mean the same bytes. A plan
			// from another payload would resume into a stitched-together file
			// whose parts all upload "successfully" and cannot be played.
			logf("resume: the plan was built from %q, not %q; starting fresh",
				loaded.Source, src.Describe())
			plan = Plan{}
		default:
			plan = loaded
			for _, p := range loaded.Parts {
				if p.SHA256 != "" {
					skip[p.Number] = true
				}
			}
			logf("resume: %d of %d parts already stored",
				len(skip), len(loaded.Parts))
		}
	}
	if len(plan.Parts) == 0 {
		plan = buildPlan(src.Name(), src.Describe(), src.Size(), int64(chunk))
	}

	// Hash whatever is about to be sent, so the manifest carries real digests
	// and a restore can prove what it got back. One pass over the payload: the
	// part digests and the source digest come out of the same bytes, which on a
	// URL is the difference between one download and two.
	sourceDigest, err := hashSource(src, &plan)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error hashing source: %v\n", err)
		return 2
	}
	if *planOut != "" {
		if err := writeJSON(*planOut, plan); err != nil {
			logf("warning: could not write the plan: %v", err)
		}
	}

	result := Result{
		Mode:         "upload",
		Channel:      *channel,
		Filename:     plan.Filename,
		TotalSize:    plan.TotalSize,
		Chunked:      len(plan.Parts) > 1,
		ChunkCount:   len(plan.Parts),
		Concurrency:  *concurrency,
		SourceSHA256: sourceDigest,
	}

	if *dryRun {
		for _, p := range plan.Parts {
			result.Parts = append(result.Parts, StoredPart{
				Part: p.Number, Offset: p.Offset, Size: p.Size, SHA256: p.SHA256,
				Name: partName(plan.Filename, p.Number, len(plan.Parts)),
			})
		}
		result.OK = true
		writeResult(*resultOut, result)
		fmt.Printf("dry run: %d parts planned, %s hashed, nothing sent\n",
			len(plan.Parts), humanBytes(float64(plan.TotalSize)))
		return 0
	}

	if *channel == "" {
		fmt.Fprintln(os.Stderr, "error: --channel is required")
		return 2
	}
	// No point opening more connections than there is work to send.
	if toSend := len(plan.Parts) - len(skip); *concurrency > toSend && toSend > 0 {
		logf("only %d part(s) left to send, so using %d connection(s)",
			toSend, toSend)
		*concurrency = toSend
	}

	in := bufio.NewReader(os.Stdin)
	started := time.Now()

	p, poolErr := openPool(ctx, cred, trimAt(*channel), *concurrency, in)
	if poolErr != nil {
		result.OK = false
		result.Error = poolErr.Error()
		result.ElapsedSec = time.Since(started).Seconds()
		writeResult(*resultOut, result)
		fmt.Fprintf(os.Stderr, "upload failed: %v\n", poolErr)
		return 1
	}
	defer p.close()
	result.Concurrency = len(p.conns)

	logf("sending %d part(s) over %d connection(s) to %s",
		len(plan.Parts)-len(skip), len(p.conns), *channel)

	u := newUploader(p, src, plan.Filename, *channel, *caption,
		*planOut, plan, started, len(p.conns))
	u.runAll(ctx, plan, skip)

	result = u.summary(plan, *channel)
	result.Mode = "upload"
	result.Concurrency = len(p.conns)
	result.SourceSHA256 = sourceDigest

	if len(u.failures) > 0 {
		result.OK = false
		result.Error = fmt.Sprintf("%d of %d parts failed: %v",
			len(u.failures), len(plan.Parts), u.firstFailure())
	} else if err := finaliseArchive(ctx, p, u, plan, *channel, *caption,
		sourceDigest, *thumbnail, &result); err != nil {
		result.OK = false
		result.Error = err.Error()
	}

	result.ElapsedSec = time.Since(started).Seconds()
	if result.ElapsedSec > 0 && result.SkippedParts == nil {
		result.BytesPerSec = float64(u.sentBytes.Load()) / result.ElapsedSec
	}
	writeResult(*resultOut, result)

	fmt.Println()
	fmt.Printf("elapsed     %.1fs\n", result.ElapsedSec)
	fmt.Printf("rate        %s/s over %d connection(s)\n",
		humanBytes(result.BytesPerSec), result.Concurrency)
	if result.MessageLink != "" {
		fmt.Printf("link        %s\n", result.MessageLink)
	}
	if !result.OK {
		fmt.Fprintf(os.Stderr, "upload incomplete: %s\n", result.Error)
		return 1
	}
	return 0
}

// finaliseArchive posts the manifest when there is more than one part.
//
// A single-part upload needs no manifest: the file is its own archive, and the
// Python restore path already handles that shape.
func finaliseArchive(
	ctx context.Context,
	p *pool,
	u *uploader,
	plan Plan,
	channel, caption, sourceDigest, thumbnail string,
	result *Result,
) error {
	stored := make([]StoredPart, 0, len(plan.Parts))
	u.mu.Lock()
	for _, item := range plan.Parts {
		if entry, ok := u.stored[item.Number]; ok && entry.MessageID != 0 {
			stored = append(stored, entry)
		}
	}
	u.mu.Unlock()

	if len(stored) == 0 {
		return errors.New("no part reached Telegram")
	}
	if len(stored) == 1 && len(plan.Parts) == 1 {
		result.MessageLink = stored[0].Link
		result.MessageID = stored[0].MessageID
		result.OK = true
		return nil
	}

	manifest := Manifest{
		Marker:        manifestKey,
		Version:       manifestVer,
		Filename:      plan.Filename,
		Size:          plan.TotalSize,
		ChunkSize:     plan.ChunkSize,
		Chunked:       len(plan.Parts) > 1,
		ChunkCount:    len(stored),
		Channel:       channel,
		Caption:       caption,
		SourceSHA256:  sourceDigest,
		ThumbnailPath: thumbnail,
		CreatedAt:     nowStamp(),
		App:           "T_Dubber/tgup",
		Parts:         stored,
	}
	link, id, err := postManifest(ctx, p.next(), channel, plan.Filename, manifest)
	if err != nil {
		return fmt.Errorf("manifest: %w", err)
	}
	result.MessageLink = link
	result.MessageID = id
	result.OK = true
	return nil
}
