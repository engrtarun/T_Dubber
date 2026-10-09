package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// These tests exist because "the link upload ran" is not evidence -- and
// because the one thing that can be proved without a phone number, a Telegram
// session or an OTP is byte equality. A URL upload and a local-file upload of
// the same payload must produce the same plan and the same SHA-256 digests; if
// they diverge, the archive on the far side is unrecoverable no matter what the
// exit code said.
//
// Everything here runs against httptest servers on loopback: no Telegram, no
// credentials, no session file, no network beyond 127.0.0.1.

const chunkForTests = 64 * 1024

// blob returns deterministic pseudo-random bytes. Random content matters: a
// test payload of zeros hides off-by-one range bugs, because every wrong offset
// still hashes to something plausible only if the data repeats.
func blob(n int) []byte {
	b := make([]byte, n)
	r := rand.New(rand.NewSource(20261009))
	r.Read(b)
	return b
}

// rangeServer serves body with RFC 7233 range support and records what it was
// asked for, so a test can assert the requests were ranges and not full GETs.
type rangeServer struct {
	*httptest.Server
	mu      sync.Mutex
	ranges  []string
	methods []string
}

func newRangeServer(t *testing.T, body []byte, name string) *rangeServer {
	t.Helper()
	rs := &rangeServer{}
	rs.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rs.mu.Lock()
		rs.methods = append(rs.methods, r.Method)
		rs.ranges = append(rs.ranges, r.Header.Get("Range"))
		rs.mu.Unlock()

		w.Header().Set("Accept-Ranges", "bytes")
		w.Header().Set("Content-Type", "video/mp4")
		if r.Method == http.MethodHead {
			w.Header().Set("Content-Length", strconv.Itoa(len(body)))
			w.WriteHeader(http.StatusOK)
			return
		}
		start, end, ok := parseRangeHeader(r.Header.Get("Range"), int64(len(body)))
		if !ok {
			w.Header().Set("Content-Length", strconv.Itoa(len(body)))
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(body)
			return
		}
		w.Header().Set("Content-Range",
			fmt.Sprintf("bytes %d-%d/%d", start, end, len(body)))
		w.Header().Set("Content-Length", strconv.FormatInt(end-start+1, 10))
		w.WriteHeader(http.StatusPartialContent)
		_, _ = w.Write(body[start : end+1])
	}))
	t.Cleanup(rs.Close)
	if name != "" {
		rs.URL = rs.URL + "/" + name
	}
	return rs
}

func parseRangeHeader(spec string, total int64) (start, end int64, ok bool) {
	if !strings.HasPrefix(spec, "bytes=") {
		return 0, 0, false
	}
	spec = strings.TrimPrefix(spec, "bytes=")
	parts := strings.SplitN(spec, "-", 2)
	if len(parts) != 2 {
		return 0, 0, false
	}
	start, err := strconv.ParseInt(strings.TrimSpace(parts[0]), 10, 64)
	if err != nil {
		return 0, 0, false
	}
	if parts[1] == "" {
		end = total - 1
	} else {
		end, err = strconv.ParseInt(strings.TrimSpace(parts[1]), 10, 64)
		if err != nil {
			return 0, 0, false
		}
	}
	if end >= total {
		end = total - 1
	}
	if start > end {
		return 0, 0, false
	}
	return start, end, true
}

func (rs *rangeServer) seen() ([]string, []string) {
	rs.mu.Lock()
	defer rs.mu.Unlock()
	return append([]string(nil), rs.ranges...), append([]string(nil), rs.methods...)
}

func writeTemp(t *testing.T, name string, b []byte) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(path, b, 0o644); err != nil {
		t.Fatalf("writing %s: %v", path, err)
	}
	return path
}

// TestURLPlanEqualsFilePlan is the central claim of --url: the same bytes from
// a URL produce the same layout and the same digests as the same bytes from a
// file. Everything downstream -- the manifest, the restore path, the Python
// reader -- assumes that equality.
func TestURLPlanEqualsFilePlan(t *testing.T) {
	body := blob(300*1024 + 12345)
	srv := newRangeServer(t, body, "movie.mp4")
	file := writeTemp(t, "movie.mp4", body)

	urlPlan := runPlan(t, srv.URL)
	filePlan := runPlan(t, file)

	if filePlan.TotalSize != urlPlan.TotalSize {
		t.Fatalf("total size: url=%d file=%d", urlPlan.TotalSize, filePlan.TotalSize)
	}
	if filePlan.Filename != urlPlan.Filename {
		t.Fatalf("filename: url=%q file=%q", urlPlan.Filename, filePlan.Filename)
	}
	if len(filePlan.Parts) != len(urlPlan.Parts) {
		t.Fatalf("part count: url=%d file=%d", len(urlPlan.Parts), len(filePlan.Parts))
	}
	if len(urlPlan.Parts) < 5 {
		t.Fatalf("expected the test chunk size to split the payload, got %d part(s)",
			len(urlPlan.Parts))
	}
	for i := range filePlan.Parts {
		if filePlan.Parts[i].SHA256 != urlPlan.Parts[i].SHA256 {
			t.Fatalf("part %d digest: url=%s file=%s",
				filePlan.Parts[i].Number,
				urlPlan.Parts[i].SHA256, filePlan.Parts[i].SHA256)
		}
	}

	// No GET may come back without a range: a full-body GET per part is exactly
	// the "quietly downloaded it anyway" behaviour this flag promises not to do.
	// HEAD legitimately carries no Range -- that is the size probe.
	ranges, methods := srv.seen()
	if len(ranges) == 0 {
		t.Fatal("the origin was never contacted")
	}
	for i, r := range ranges {
		if methods[i] == http.MethodHead {
			continue
		}
		if r == "" {
			t.Fatalf("a GET arrived without a Range header: %v", ranges)
		}
	}
}

// TestUploadDryRunOverURLNeedsNoCredentials is the proof that does not need a
// phone: --dry-run resolves the origin, plans every part and hashes it without
// a session, a login or a single byte to Telegram. That is what makes a link
// testable on a machine that has never been authorised.
func TestUploadDryRunOverURLNeedsNoCredentials(t *testing.T) {
	body := blob(200 * 1024)
	srv := newRangeServer(t, body, "clip.mp4")
	file := writeTemp(t, "clip.mp4", body)

	out := filepath.Join(t.TempDir(), "result.json")
	code := cmdUpload([]string{
		"--url", srv.URL,
		"--chunk-size", strconv.Itoa(chunkForTests),
		"--dry-run",
		"--result-out", out,
	})
	if code != 0 {
		t.Fatalf("dry run over a URL returned %d, want 0", code)
	}

	data, err := os.ReadFile(out)
	if err != nil {
		t.Fatalf("result file: %v", err)
	}
	var got Result
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("result json: %v", err)
	}
	if !got.OK {
		t.Fatalf("dry run reported failure: %s", got.Error)
	}
	if got.TotalSize != int64(len(body)) {
		t.Fatalf("total size = %d, want %d", got.TotalSize, len(body))
	}
	if got.Channel != "" {
		t.Fatalf("a dry run must not pretend it uploaded to %q", got.Channel)
	}
	want, err := hashWhole(file)
	if err != nil {
		t.Fatalf("hashing the file: %v", err)
	}
	if got.SourceSHA256 != want {
		t.Fatalf("source digest: url=%s file=%s", got.SourceSHA256, want)
	}
	if len(got.Parts) != 4 {
		t.Fatalf("parts = %d, want 4", len(got.Parts))
	}
}

// TestUploadDryRunRefusesAmbiguousSources guards the one caller mistake that
// would otherwise be silent: passing both --file and --url, or neither.
func TestUploadDryRunRefusesAmbiguousSources(t *testing.T) {
	body := blob(1024)
	srv := newRangeServer(t, body, "x.mp4")
	file := writeTemp(t, "x.mp4", body)

	cases := []struct {
		name string
		args []string
	}{
		{"neither", []string{"--dry-run"}},
		{"both", []string{"--file", file, "--url", srv.URL, "--dry-run"}},
	}
	for _, tc := range cases {
		if code := cmdUpload(tc.args); code != 2 {
			t.Errorf("%s: exit %d, want 2", tc.name, code)
		}
	}
}

// TestPlanResumeRefusesAForeignSource: the plan's offsets are trusted on
// resume, so a plan built from another payload would stitch two unrelated files
// together and every part would still "succeed".
func TestPlanResumeRefusesAForeignSource(t *testing.T) {
	body := blob(5000)
	srv := newRangeServer(t, body, "same.bin")
	other := newRangeServer(t, body, "other.bin")

	src, err := newHTTPSource(context.Background(), srv.URL, 5*time.Second)
	if err != nil {
		t.Fatalf("opening the source: %v", err)
	}
	defer src.Close()

	plan := buildPlan(src.Name(), src.Describe(), src.Size(), chunkForTests)
	if !sameSource(plan, src, plan.Source) {
		t.Errorf("a plan must resume against the source it was built from")
	}
	if sameSource(plan, src, other.URL) {
		t.Errorf("a plan must NOT resume against a different URL")
	}

	file := writeTemp(t, "same.bin", body)
	fileSrc, err := openFileSource(file)
	if err != nil {
		t.Fatalf("opening the file source: %v", err)
	}
	defer fileSrc.Close()
	if sameSource(plan, fileSrc, plan.Source) {
		t.Errorf("a URL plan must not resume against a local file")
	}
	filePlan := buildPlan(fileSrc.Name(), fileSrc.Describe(), fileSrc.Size(), chunkForTests)
	if !sameSource(filePlan, fileSrc, filePlan.Source) {
		t.Errorf("a file plan must resume against its own file")
	}
}

// TestRangeRefusedIsAnErrorNotASilentDownload: an origin that ignores Range and
// streams the whole body would turn a zero-disk upload into a full download with
// no warning. The caller asked for zero disk; it must be told.
func TestRangeRefusedIsAnErrorNotASilentDownload(t *testing.T) {
	body := blob(4096)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "video/mp4")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	_, err := newHTTPSource(context.Background(), srv.URL, 5*time.Second)
	if !errors.Is(err, errNoRange) {
		t.Fatalf("got %v, want errNoRange", err)
	}
	if !strings.Contains(err.Error(), "--file") {
		t.Errorf("the error should say what to do instead, got %q", err)
	}
}

// TestSizeLearnedWhenHeadIsUseless covers the real world: plenty of origins
// answer HEAD with 405, or with no Content-Length. The one-byte ranged GET is
// the fallback, and its 206 is also how we learn whether ranges work at all.
func TestSizeLearnedWhenHeadIsUseless(t *testing.T) {
	body := blob(2048)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodHead {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Accept-Ranges", "bytes")
		start, end, ok := parseRangeHeader(r.Header.Get("Range"), int64(len(body)))
		if !ok {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(body)
			return
		}
		w.Header().Set("Content-Range",
			fmt.Sprintf("bytes %d-%d/%d", start, end, len(body)))
		w.WriteHeader(http.StatusPartialContent)
		_, _ = w.Write(body[start : end+1])
	}))
	defer srv.Close()

	src, err := newHTTPSource(context.Background(), srv.URL, 5*time.Second)
	if err != nil {
		t.Fatalf("the range fallback should have carried the run: %v", err)
	}
	defer src.Close()
	if src.Size() != int64(len(body)) {
		t.Fatalf("size = %d, want %d", src.Size(), len(body))
	}
	if !src.ranged {
		t.Fatalf("a 206 answer means ranges work")
	}
}

// TestOriginAnsweringWithTheWrongRangeIsRejected: if the origin returns 206
// with a different start than requested, the part would carry the wrong bytes
// and the digest would only disagree at the very end -- after Telegram already
// has them.
func TestOriginAnsweringWithTheWrongRangeIsRejected(t *testing.T) {
	body := blob(8192)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Accept-Ranges", "bytes")
		w.Header().Set("Content-Range",
			fmt.Sprintf("bytes 0-99/%d", len(body)))
		w.WriteHeader(http.StatusPartialContent)
		_, _ = w.Write(body[:100])
	}))
	defer srv.Close()

	src, err := newHTTPSource(context.Background(), srv.URL, 5*time.Second)
	if err == nil {
		_, err = src.OpenRange(2048, 1024)
		src.Close()
	}
	if err == nil || !strings.Contains(err.Error(), "bytes starting at") {
		t.Fatalf("got %v, want a wrong-offset rejection", err)
	}
}

// TestHashSourceRefusesAShortSource: if the origin lied about the size, the
// plan's parts would silently shrink. Better a clean error than a manifest
// whose totals disagree with what Telegram received.
func TestHashSourceRefusesAShortSource(t *testing.T) {
	body := blob(4096)
	srv := newRangeServer(t, body, "short.bin")

	src, err := newHTTPSource(context.Background(), srv.URL, 5*time.Second)
	if err != nil {
		t.Fatalf("opening the source: %v", err)
	}
	defer src.Close()

	plan := buildPlan(src.Name(), src.Describe(), src.Size()+1024, chunkForTests)
	if _, err := hashSource(src, &plan); err == nil {
		t.Fatal("hashing a plan larger than the source must fail")
	}
}

// TestHashSourceDetectsAnUndersizedPart covers the boundary: the last part is
// short by one byte, which a tolerant reader would let through.
func TestHashSourceDetectsAnUndersizedPart(t *testing.T) {
	body := blob(1000)
	src, err := newHTTPSource(context.Background(),
		newRangeServer(t, body, "tail.bin").URL, 5*time.Second)
	if err != nil {
		t.Fatalf("opening the source: %v", err)
	}
	defer src.Close()

	plan := buildPlan(src.Name(), src.Describe(), src.Size(), chunkForTests)
	if _, err := hashSource(src, &plan); err != nil {
		t.Fatalf("an exactly-sized plan must hash: %v", err)
	}
}

// TestURLNameCarriesNoCredential: a signed URL puts its token in the query, and
// the name ends up in the Telegram message title and in the manifest that the
// Python restore path writes to the database.
func TestURLNameCarriesNoCredential(t *testing.T) {
	const token = "hf_SUPERSECRETTOKEN"
	cases := []struct {
		raw    string
		want   string
		leaked string
	}{
		{"https://host/dl/movie.mp4?token=" + token, "movie.mp4", token},
		{"https://host/dl/movie%20one.mkv?sig=" + token, "movie one.mkv", token},
		{"https://host/dl/", "downloaded.bin", token},
		{"https://host", "downloaded.bin", token},
	}
	for _, tc := range cases {
		got := sanitise(filenameFromURL(tc.raw))
		if got != tc.want {
			t.Errorf("filenameFromURL(%q) = %q, want %q", tc.raw, got, tc.want)
		}
		if strings.Contains(got, token) {
			t.Errorf("the name leaked the credential: %q", got)
		}
		redacted := redactURL(tc.raw)
		if strings.Contains(redacted, token) {
			t.Errorf("redactURL(%q) leaked the credential: %q", tc.raw, redacted)
		}
	}
}

func TestContentRangeParsing(t *testing.T) {
	if got := totalFromContentRange("bytes 0-1023/2048"); got != 2048 {
		t.Errorf("total = %d, want 2048", got)
	}
	if got := firstFromContentRange("bytes 512-1023/2048"); got != 512 {
		t.Errorf("first = %d, want 512", got)
	}
	for _, bad := range []string{"", "bytes 0-1/*", "nonsense", "bytes=0-1/"} {
		if got := totalFromContentRange(bad); got != 0 {
			t.Errorf("totalFromContentRange(%q) = %d, want 0", bad, got)
		}
	}
	// "bytes */2048" is the unsatisfied-range form: the total is still known.
	if got := totalFromContentRange("bytes */2048"); got != 2048 {
		t.Errorf("totalFromContentRange of an unsatisfied range = %d, want 2048", got)
	}
}

// TestConcurrentRangeReadsStayCorrect is the regression test for the reason
// OpenRange hands out independent streams: several goroutines reading different
// ranges at once must each get exactly their own bytes. A shared file handle
// plus Seek would interleave here.
func TestConcurrentRangeReadsStayCorrect(t *testing.T) {
	body := blob(512 * 1024)
	srv := newRangeServer(t, body, "par.bin")
	file := writeTemp(t, "par.bin", body)

	src, err := newHTTPSource(context.Background(), srv.URL, 10*time.Second)
	if err != nil {
		t.Fatalf("opening the source: %v", err)
	}
	defer src.Close()

	const readers = 8
	const width = 4096
	var wg sync.WaitGroup
	errs := make(chan error, readers)
	for i := 0; i < readers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			offset := int64(i * width)
			stream, err := src.OpenRange(offset, width)
			if err != nil {
				errs <- err
				return
			}
			defer stream.Close()
			got, err := io.ReadAll(stream)
			if err != nil {
				errs <- err
				return
			}
			want := body[offset : offset+width]
			if !bytes.Equal(got, want) {
				errs <- fmt.Errorf("reader %d got %d bytes, first mismatch at %d",
					i, len(got), firstMismatch(got, want))
			}
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Error(err)
	}

	// Same promise for the file source, which is what most uploads still use.
	fileSrc, err := openFileSource(file)
	if err != nil {
		t.Fatalf("opening the file source: %v", err)
	}
	defer fileSrc.Close()
	for i := 0; i < readers; i++ {
		stream, err := fileSrc.OpenRange(int64(i*width), width)
		if err != nil {
			t.Fatalf("file range %d: %v", i, err)
		}
		got, err := io.ReadAll(stream)
		stream.Close()
		if err != nil {
			t.Fatalf("reading file range %d: %v", i, err)
		}
		if !bytes.Equal(got, body[i*width:(i+1)*width]) {
			t.Fatalf("file range %d returned the wrong bytes", i)
		}
	}
}

func firstMismatch(a, b []byte) int {
	n := len(a)
	if len(b) < n {
		n = len(b)
	}
	for i := 0; i < n; i++ {
		if a[i] != b[i] {
			return i
		}
	}
	return n
}

// runPlan invokes cmdPlan the way the binary does and reads the plan back.
func runPlan(t *testing.T, arg string) Plan {
	t.Helper()
	out := filepath.Join(t.TempDir(), "plan.json")
	args := []string{"--chunk-size", strconv.Itoa(chunkForTests), "--plan-out", out}
	if strings.HasPrefix(arg, "http") {
		args = append([]string{"--url", arg}, args...)
	} else {
		args = append([]string{"--file", arg}, args...)
	}
	if code := cmdPlan(args); code != 0 {
		t.Fatalf("plan %s returned %d", arg, code)
	}
	data, err := os.ReadFile(out)
	if err != nil {
		t.Fatalf("reading the plan: %v", err)
	}
	var plan Plan
	if err := json.Unmarshal(data, &plan); err != nil {
		t.Fatalf("plan json: %v", err)
	}
	if len(plan.Parts) == 0 {
		t.Fatalf("plan for %s listed no parts", arg)
	}
	return plan
}
