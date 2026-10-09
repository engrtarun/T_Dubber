package main

// source.go -- where the bytes come from.
//
// Before this file, every code path that touched the payload asked for a
// filesystem path: upload.go did os.Open + Seek, main.go's hashRange/hashWhole
// did the same, and commands.go stat'ed the file to learn its size. That is why
// "upload straight from a link" could never exist as more than a plan: the
// shape of the code demanded a local file, so link -> disk -> Telegram was the
// only route, and a 9 GB movie became 9 GB on disk before a single byte
// reached Telegram.
//
// This file introduces one interface with two implementations:
//
//	fileSource  the old behaviour, unchanged
//	httpSource  a ranged GET per part, nothing written to disk
//
// Both hand out byte ranges as streams, so everything downstream (hashing,
// chunking, the manifest, the Python caller) stays identical. A URL upload and
// a file upload produce the same plan and the same digests -- that equality is
// what source_test.go asserts, because "it ran" is not evidence, "the bytes
// match" is.
//
// Three things this deliberately does NOT do, each because the alternative
// silently wastes a user's uplink:
//
//  1. No spooling. If the server cannot serve byte ranges, we fail instead of
//     quietly downloading the whole body to a temp file -- a caller who asked
//     for zero-disk would never learn it got a disk.
//  2. No re-probing per part. Size and range support are learned once, up
//     front, and reused; a 5-part upload makes one probe, not five.
//  3. No trusting the plan file blindly across a URL change. A signed URL can
//     expire mid-run, so the resume path refuses to reuse a plan whose source
//     no longer describes the same bytes (see sameSource).

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// errNoRange is returned when a server will not serve byte ranges. It is a
// typed error because the message we print for it is advice, not a stack trace:
// the fix is a different source, not a different flag.
var errNoRange = errors.New("the server does not support HTTP range requests " +
	"(a range probe returned 200 instead of 206 and Accept-Ranges is absent); " +
	"download the file first and use --file")

// source is a seekable, sizeable payload: a local file today, an http(s) URL
// tomorrow. OpenRange returns exactly size bytes starting at offset, as a
// stream -- nothing is buffered whole, so a 1.9 GB part costs a few megabytes.
type source interface {
	// OpenRange streams [offset, offset+size) from the source.
	OpenRange(offset, size int64) (io.ReadCloser, error)
	// Size is the total payload length in bytes, known before any byte moves.
	Size() int64
	// Name is what Telegram and the manifest will call it.
	Name() string
	// Describe is for humans in log lines; it may include credentials-free
	// origin detail, unlike Name.
	Describe() string
	// Close releases whatever the source is holding (file handles).
	Close() error
}

// ---------------------------------------------------------------------------
// fileSource
// ---------------------------------------------------------------------------

// fileSource is the local-file case, and deliberately the boring one: same
// behaviour as before this file existed, so --file keeps its exact semantics.
type fileSource struct {
	path string
	f    *os.File
	size int64
}

func openFileSource(path string) (*fileSource, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	stat, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, err
	}
	if stat.Size() == 0 {
		f.Close()
		return nil, errors.New("refusing to upload a 0-byte file")
	}
	return &fileSource{path: path, f: f, size: stat.Size()}, nil
}

// OpenRange seeks the shared handle and hands back a limited reader. Seek+Read
// on a *os.File is safe against concurrent Seek only because every caller gets
// its own handle; see openRangeIndependent.
func (s *fileSource) OpenRange(offset, size int64) (io.ReadCloser, error) {
	return openRangeIndependent(s.path, offset, size)
}

// openRangeIndependent gives each range its own file handle. Upload workers run
// concurrently, so sharing one handle and seeking it from several goroutines
// would interleave reads from the wrong offsets -- the exact class of bug that
// produced mismatched digests before the Content-Length was verified.
func openRangeIndependent(path string, offset, size int64) (io.ReadCloser, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		f.Close()
		return nil, err
	}
	return &limitedFile{f: f, r: io.LimitReader(f, size)}, nil
}

type limitedFile struct {
	f *os.File
	r io.Reader
}

func (l *limitedFile) Read(p []byte) (int, error) { return l.r.Read(p) }
func (l *limitedFile) Close() error               { return l.f.Close() }

func (s *fileSource) Size() int64      { return s.size }
func (s *fileSource) Name() string     { return filepath.Base(s.path) }
func (s *fileSource) Describe() string { return s.path }
func (s *fileSource) Close() error     { return s.f.Close() }

// ---------------------------------------------------------------------------
// httpSource
// ---------------------------------------------------------------------------

// httpSource streams byte ranges from an http(s) URL.
//
// Two fields carry the whole design: size is learned once from the origin, and
// ranged says whether the origin honoured a range probe. Everything after that
// is arithmetic.
type httpSource struct {
	url    string
	name   string
	size   int64
	ranged bool
	client *http.Client

	// sem bounds concurrent range GETs to the number of upload connections the
	// program is allowed to open anyway. It waits rather than refusing: the
	// uploader runs up to maxConcurrency parts at once, so a limit that
	// rejected the 5th request would fail every ordinary multi-part upload. An
	// origin that will not keep up fails on its own timeouts, which is a
	// truthful error; inventing one here would only hide the real cause.
	sem chan struct{}
}

func newHTTPSource(ctx context.Context, raw string, timeout time.Duration) (*httpSource, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return nil, fmt.Errorf("bad --url: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return nil, fmt.Errorf("bad --url: scheme %q is not http or https", u.Scheme)
	}
	if u.Host == "" {
		return nil, errors.New("bad --url: no host")
	}
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	s := &httpSource{
		url:    raw,
		client: &http.Client{Timeout: timeout},
		sem:    make(chan struct{}, maxConcurrency),
	}
	if err := s.probe(ctx); err != nil {
		return nil, err
	}
	return s, nil
}

// probe learns the size and whether ranges work.
//
// Order matters. HEAD is the cheapest answer but plenty of origins answer it
// with 405 or with no Content-Length, and some CDN front-ends answer HEAD from
// cache with a stale length. So: try HEAD, and if the size is still unknown --
// or if HEAD failed outright -- fall back to a one-byte ranged GET, whose 206
// Content-Range carries the authoritative total.
func (s *httpSource) probe(ctx context.Context) error {
	if size, ranged, err := s.head(ctx); err == nil && size > 0 {
		s.size = size
		s.ranged = ranged
		if !s.ranged {
			return errNoRange
		}
	} else {
		size, ranged, ferr := s.probeRange(ctx)
		if ferr != nil {
			if err != nil {
				return err
			}
			return ferr
		}
		s.size = size
		s.ranged = ranged
		if !s.ranged {
			return errNoRange
		}
	}
	if s.size <= 0 {
		return fmt.Errorf("could not learn the size of %s: the origin reported no "+
			"Content-Length", redactURL(s.url))
	}
	s.name = sanitise(filenameFromURL(s.url))
	if s.name == "downloaded.bin" {
		// No usable name in the path: keep the extension the origin advertises
		// if it gave one, otherwise the generic fallback is honest.
		if ext := extensionFromContentType(s.client, s.url); ext != "" {
			s.name = "download" + ext
		}
	}
	return nil
}

func (s *httpSource) head(ctx context.Context) (size int64, ranged bool, err error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, s.url, nil)
	if err != nil {
		return 0, false, err
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return 0, false, err
	}
	defer drain(resp)
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return 0, false, fmt.Errorf("HEAD %s: %s", redactURL(s.url), resp.Status)
	}
	size = resp.ContentLength
	if size <= 0 {
		if raw := resp.Header.Get("Content-Length"); raw != "" {
			size, _ = strconv.ParseInt(raw, 10, 64)
		}
	}
	ranged = strings.EqualFold(resp.Header.Get("Accept-Ranges"), "bytes")
	return size, ranged, nil
}

// probeRange asks for exactly one byte. A 206 answers both questions at once:
// that ranges work, and (through Content-Range) how big the payload really is.
func (s *httpSource) probeRange(ctx context.Context) (size int64, ranged bool, err error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.url, nil)
	if err != nil {
		return 0, false, err
	}
	req.Header.Set("Range", "bytes=0-0")
	resp, err := s.client.Do(req)
	if err != nil {
		return 0, false, err
	}
	defer drain(resp)
	switch {
	case resp.StatusCode == http.StatusPartialContent:
		size = totalFromContentRange(resp.Header.Get("Content-Range"))
		if size <= 0 {
			return 0, false, fmt.Errorf("range probe of %s returned 206 with no "+
				"usable Content-Range", redactURL(s.url))
		}
		return size, true, nil
	case resp.StatusCode == http.StatusOK:
		// The origin ignored Range and is streaming the whole body. Reading it
		// would defeat the point of the flag, so report the shortfall instead.
		return 0, false, errNoRange
	default:
		return 0, false, fmt.Errorf("range probe of %s: %s",
			redactURL(s.url), resp.Status)
	}
}

// OpenRange streams one part. The Content-Range of the reply is checked against
// what was asked for: an origin that quietly returns 200 (the whole body) for a
// ranged request would otherwise stream the entire file into one part's slot,
// and the part digest would only disagree with the plan at the very end --
// after the bytes were already on their way to Telegram.
func (s *httpSource) OpenRange(offset, size int64) (io.ReadCloser, error) {
	if !s.ranged {
		return nil, errNoRange
	}
	// Bounded by maxConcurrency, the same ceiling the connection pool has.
	// This waits instead of refusing: the uploader runs up to maxConcurrency
	// parts at once, so rejecting the 5th request would fail every ordinary
	// multi-part upload. Waiting cannot hang forever either -- every slot is
	// held by a body read, and the client timeout covers the whole body.
	s.sem <- struct{}{}
	release := func() { <-s.sem }
	req, err := http.NewRequest(http.MethodGet, s.url, nil)
	if err != nil {
		release()
		return nil, err
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", offset, offset+size-1))
	resp, err := s.client.Do(req)
	if err != nil {
		release()
		return nil, err
	}
	if resp.StatusCode != http.StatusPartialContent {
		status := resp.Status
		drain(resp)
		release()
		if resp.StatusCode == http.StatusOK {
			return nil, errNoRange
		}
		return nil, fmt.Errorf("GET range %d-%d of %s: %s",
			offset, offset+size-1, redactURL(s.url), status)
	}
	if got := firstFromContentRange(resp.Header.Get("Content-Range")); got != offset {
		drain(resp)
		release()
		return nil, fmt.Errorf("origin answered range %d-%d with bytes starting at %d",
			offset, offset+size-1, got)
	}
	return &rangeBody{resp: resp, onClose: release}, nil
}

type rangeBody struct {
	resp    *http.Response
	onClose func()
	once    sync.Once
}

func (r *rangeBody) Read(p []byte) (int, error) { return r.resp.Body.Read(p) }
func (r *rangeBody) Close() error {
	r.once.Do(func() {
		drain(r.resp)
		if r.onClose != nil {
			r.onClose()
		}
	})
	return nil
}

func (s *httpSource) Size() int64      { return s.size }
func (s *httpSource) Name() string     { return s.name }
func (s *httpSource) Describe() string { return redactURL(s.url) }
func (s *httpSource) Close() error     { s.client.CloseIdleConnections(); return nil }

// ---------------------------------------------------------------------------
// URL helpers
// ---------------------------------------------------------------------------

// filenameFromURL takes the last path segment and nothing else. The query
// string is dropped on purpose: a signed URL puts its credential there, and
// that string ends up in the Telegram message name and in the manifest.
func filenameFromURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	// A trailing slash means the URL names a directory, not a file. path.Base
	// would helpfully return the last directory name ("dl") and Telegram would
	// then receive a movie called "dl".
	if u.Path == "" || strings.HasSuffix(u.Path, "/") {
		return ""
	}
	seg := path.Base(u.Path)
	if seg == "/" || seg == "." || seg == ".." {
		return ""
	}
	if decoded, derr := url.PathUnescape(seg); derr == nil {
		seg = decoded
	}
	return seg
}

// redactURL strips the query and any userinfo, so a log line or a Describe()
// string can never carry a token.
func redactURL(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return "<url>"
	}
	u.RawQuery = ""
	u.Fragment = ""
	u.User = nil
	return u.String()
}

func totalFromContentRange(cr string) int64 {
	if cr == "" {
		return 0
	}
	slash := strings.LastIndex(cr, "/")
	if slash < 0 {
		return 0
	}
	total := strings.TrimSpace(cr[slash+1:])
	if total == "*" {
		return 0
	}
	n, err := strconv.ParseInt(total, 10, 64)
	if err != nil {
		return 0
	}
	return n
}

// firstFromContentRange returns the first byte offset of a Content-Range reply,
// or -1 when the header is absent or unintelligible.
//
// The unit prefix is stripped before the numbers are read: "bytes 512-1023/2048"
// starts at 512, and parsing "bytes" as the range spec is how an origin that
// answered perfectly well gets reported as lying.
func firstFromContentRange(cr string) int64 {
	spec, ok := contentRangeSpec(cr)
	if !ok {
		return -1
	}
	dash := strings.Index(spec, "-")
	if dash < 0 {
		return -1
	}
	n, err := strconv.ParseInt(strings.TrimSpace(spec[:dash]), 10, 64)
	if err != nil {
		return -1
	}
	return n
}

// contentRangeSpec strips the leading unit token ("bytes" / "bytes="), leaving
// "start-end/total" behind.
func contentRangeSpec(cr string) (string, bool) {
	fields := strings.Fields(strings.TrimSpace(cr))
	if len(fields) == 0 {
		return "", false
	}
	// "bytes=0-99" and "bytes 0-99/100" are both legal; so is "bytes * /100".
	first := strings.TrimPrefix(strings.TrimPrefix(fields[0], "bytes="), "bytes")
	if first == "" {
		if len(fields) < 2 {
			return "", false
		}
		first = fields[1]
	}
	if first == "*" {
		return "", false
	}
	return first, true
}

func extensionFromContentType(client *http.Client, raw string) string {
	resp, err := client.Head(raw)
	if err != nil {
		return ""
	}
	defer drain(resp)
	ct := resp.Header.Get("Content-Type")
	if i := strings.Index(ct, ";"); i >= 0 {
		ct = ct[:i]
	}
	switch strings.TrimSpace(strings.ToLower(ct)) {
	case "video/mp4":
		return ".mp4"
	case "video/x-matroska":
		return ".mkv"
	case "video/webm":
		return ".webm"
	case "video/quicktime":
		return ".mov"
	case "audio/mpeg":
		return ".mp3"
	}
	return ""
}

func drain(resp *http.Response) {
	if resp == nil || resp.Body == nil {
		return
	}
	io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
	resp.Body.Close()
}

// ---------------------------------------------------------------------------
// Hashing
// ---------------------------------------------------------------------------

// hashSource walks the payload once and produces both digests the plan needs:
// every part's SHA-256 and the whole source's SHA-256.
//
// One pass, not one-per-part plus one-whole. The old code hashed each part and
// then hashed the file again (commands.go), which is free on a local file and
// costs an extra full download on a URL. Streaming once and feeding each part's
// hasher and the source hasher from the same bytes keeps the arithmetic
// identical while making the URL case pay for one pass instead of two.
//
// Parts that arrive from --plan-in already carrying a digest are still consumed
// (their bytes must be skipped in the stream) but are not re-hashed, which is
// what makes resume cheap.
func hashSource(src source, plan *Plan) (string, error) {
	reader, err := src.OpenRange(0, plan.TotalSize)
	if err != nil {
		return "", err
	}
	defer reader.Close()

	whole := sha256.New()
	for i := range plan.Parts {
		p := &plan.Parts[i]
		part := sha256.New()
		writers := []io.Writer{whole}
		if p.SHA256 == "" {
			writers = append(writers, part)
		}
		n, err := io.Copy(io.MultiWriter(writers...), io.LimitReader(reader, p.Size))
		if err != nil {
			return "", fmt.Errorf("hashing part %d: %w", p.Number, err)
		}
		if n != p.Size {
			return "", fmt.Errorf("part %d wanted %d bytes but the source offered %d",
				p.Number, p.Size, n)
		}
		if p.SHA256 == "" {
			p.SHA256 = hex.EncodeToString(part.Sum(nil))
		}
	}
	// Anything past the last part means the size learned during probing was
	// wrong. Better to stop now than to post a manifest whose total does not
	// match what Telegram actually received.
	if extra, err := io.Copy(io.Discard, reader); err != nil {
		return "", err
	} else if extra > 0 {
		return "", fmt.Errorf("the source is %d bytes longer than the %d bytes planned",
			extra, plan.TotalSize)
	}
	return hex.EncodeToString(whole.Sum(nil)), nil
}

// sameSource reports whether a saved plan describes the payload now in hand.
// Resume trusts the plan's offsets, so a plan built from a different URL would
// stitch together bytes from two different files -- every part would "succeed"
// and the archive would be unplayable. A local file is identified by its path;
// a URL by its redacted form, because a signed URL's token changes while the
// payload does not.
func sameSource(plan Plan, src source, savedFrom string) bool {
	switch s := src.(type) {
	case *fileSource:
		if savedFrom == "" {
			return false
		}
		abs, err := filepath.Abs(s.path)
		if err != nil {
			return false
		}
		savedAbs, err := filepath.Abs(savedFrom)
		if err != nil {
			return false
		}
		return abs == savedAbs
	case *httpSource:
		if savedFrom == "" {
			return false
		}
		return redactURL(savedFrom) == s.Describe()
	}
	return false
}
