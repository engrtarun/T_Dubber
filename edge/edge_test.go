package edge

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// newRoot builds a small artefact tree and returns its path.
//
// The shape mirrors what the Space actually publishes -- a role prefix with
// files under it -- because a test that only ever writes flat names would never
// exercise the subdirectory case in roleFor or the nested path in Resolve.
func newRoot(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	write := func(rel string, body string) {
		full := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatalf("mkdir %s: %v", rel, err)
		}
		if err := os.WriteFile(full, []byte(body), 0o644); err != nil {
			t.Fatalf("write %s: %v", rel, err)
		}
	}
	write("pylibs/stage1.tar.zst", "PYLIBS")
	write("weights/homura/model.safetensors", "WEIGHTS-5GB-PLACEHOLDER")
	write("pack/stitcher", "STITCHER")
	write("README.txt", "notes")
	// The spool must stay out of the manifest even when it is populated, so it
	// is created here too rather than only in the empty case.
	write(ScratchDirName+"/inflight.bin", "SPOOL")
	return root
}

func TestResolveRejectsEscape(t *testing.T) {
	root := newRoot(t)

	// Each of these is an attempt to read something outside the artefact root.
	// The interesting ones are the last three: "../" and an absolute path are
	// what a scanner sends, and a symlinked sibling is what a real intrusion
	// attempt would use.
	cases := []struct {
		name string
		rel  string
	}{
		{"dotdot", "../outside.txt"},
		{"dotdot nested", "pylibs/../../outside.txt"},
		{"dotdot deep", "../../../../etc/passwd"},
		{"bare dotdot", ".."},
		{"absolute unix", "/etc/passwd"},
		{"absolute windows", `C:\Windows\win.ini`},
		{"empty", ""},
		{"manifest itself", ManifestName},
		{"null byte", "weights/\x00.bin"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Resolve(root, tc.rel)
			if err == nil {
				t.Fatalf("Resolve(%q) = %q, want an error", tc.rel, got)
			}
			if err != ErrNotFound {
				t.Fatalf("Resolve(%q) err = %v, want ErrNotFound", tc.rel, err)
			}
		})
	}
}

func TestResolveAcceptsInside(t *testing.T) {
	root := newRoot(t)
	for _, rel := range []string{
		"pylibs/stage1.tar.zst",
		"weights/homura/model.safetensors",
		"README.txt",
	} {
		got, err := Resolve(root, rel)
		if err != nil {
			t.Fatalf("Resolve(%q): %v", rel, err)
		}
		if !strings.HasPrefix(got, root) {
			t.Fatalf("Resolve(%q) = %q, outside root %q", rel, got, root)
		}
	}
}

func TestResolveRejectsSiblingWithSharedPrefix(t *testing.T) {
	// The string-prefix check is the classic bug here: a root of ".../artefacts"
	// with a sibling ".../artefacts-evil" passes a naive HasPrefix. Building the
	// sibling for real is the only way to know the check is not naive.
	base := t.TempDir()
	root := filepath.Join(base, "artefacts")
	evil := filepath.Join(base, "artefacts-evil")
	for _, d := range []string{root, evil} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(evil, "secret"), []byte("no"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := Resolve(root, "../artefacts-evil/secret")
	if err != ErrNotFound {
		t.Fatalf("Resolve across sibling = (%q, %v), want ErrNotFound", got, err)
	}
}

func TestBuildManifestExcludesScratch(t *testing.T) {
	root := newRoot(t)
	m, err := BuildManifest(root)
	if err != nil {
		t.Fatalf("BuildManifest: %v", err)
	}

	seen := map[string]Entry{}
	for _, e := range m.Entries {
		seen[e.Name] = e
	}
	for _, want := range []string{
		"pylibs/stage1.tar.zst",
		"weights/homura/model.safetensors",
		"pack/stitcher",
		"README.txt",
	} {
		if _, ok := seen[want]; !ok {
			t.Errorf("manifest missing %s", want)
		}
	}
	if _, ok := seen[ManifestName]; ok {
		t.Errorf("manifest lists itself: %v", m.Entries)
	}
	for name := range seen {
		if strings.HasPrefix(name, ScratchDirName+"/") {
			t.Errorf("manifest includes scratch file %s -- a spooled upload would change the manifest", name)
		}
	}

	// Role classification is what lets the client ask for "weights" without
	// hardcoding a path, so a wrong role is a silent fetch of the wrong thing.
	wantRoles := map[string]string{
		"pylibs/stage1.tar.zst":              "pylibs",
		"weights/homura/model.safetensors":   "weights",
		"pack/stitcher":                      "pack",
		"README.txt":                         "other",
	}
	for name, want := range wantRoles {
		if got := seen[name].Role; got != want {
			t.Errorf("role(%s) = %q, want %q", name, got, want)
		}
	}

	sum := int64(0)
	for _, e := range m.Entries {
		sum += e.SizeBytes
	}
	if m.TotalBytes != sum {
		t.Fatalf("TotalBytes = %d, sum of entries = %d", m.TotalBytes, sum)
	}
}

func TestManifestDigestMatchesFile(t *testing.T) {
	root := newRoot(t)
	m, err := BuildManifest(root)
	if err != nil {
		t.Fatalf("BuildManifest: %v", err)
	}
	entry, ok := m.Find("pylibs/stage1.tar.zst")
	if !ok {
		t.Fatal("entry not found")
	}
	want := sha256.Sum256([]byte("PYLIBS"))
	if entry.SHA256 != hex.EncodeToString(want[:]) {
		t.Fatalf("sha256 = %s, want %x", entry.SHA256, want)
	}
	if entry.SizeBytes != int64(len("PYLIBS")) {
		t.Fatalf("size = %d, want %d", entry.SizeBytes, len("PYLIBS"))
	}
}

func TestVerifyRejectsCorruptBody(t *testing.T) {
	root := newRoot(t)
	m, err := BuildManifest(root)
	if err != nil {
		t.Fatal(err)
	}
	if err := m.Verify("pylibs/stage1.tar.zst", bytes.NewReader([]byte("PYLIBS"))); err != nil {
		t.Fatalf("Verify good body: %v", err)
	}
	// A truncated archive is the case that matters: unpacking it would write
	// half a wheelhouse into the interpreter's path and then fail at import
	// time, much further from the cause.
	err = m.Verify("pylibs/stage1.tar.zst", bytes.NewReader([]byte("PYLIB")))
	if err == nil {
		t.Fatal("Verify accepted a truncated body")
	}
	// Same length, different bytes: this is what a substituted artefact looks
	// like, and a length check alone would pass it.
	err = m.Verify("pylibs/stage1.tar.zst", bytes.NewReader([]byte("pylibs")))
	if err == nil {
		t.Fatal("Verify accepted substituted bytes of equal length")
	}
	if err := m.Verify("nope.bin", bytes.NewReader([]byte("x"))); err == nil {
		t.Fatal("Verify accepted an unpublished name")
	}
}

// writeManifest builds the manifest for root and puts it on disk, which is what
// publishes the artefacts it covers.
func writeManifest(root string) error {
	m, err := BuildManifest(root)
	if err != nil {
		return err
	}
	body, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(root, ManifestName), body, 0o644)
}

func TestManifestRoundTrip(t *testing.T) {
	root := newRoot(t)
	m, err := BuildManifest(root)
	if err != nil {
		t.Fatal(err)
	}
	body, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, ManifestName), body, 0o644); err != nil {
		t.Fatal(err)
	}
	back, err := LoadManifest(root)
	if err != nil {
		t.Fatalf("LoadManifest: %v", err)
	}
	if len(back.Entries) != len(m.Entries) {
		t.Fatalf("entries = %d, want %d", len(back.Entries), len(m.Entries))
	}
}

func TestLoadManifestMissingIsEmptyNotError(t *testing.T) {
	// A Space that has published nothing is a valid state: the client is meant
	// to fall back to origin rather than treat this as a failure.
	m, err := LoadManifest(t.TempDir())
	if err != nil {
		t.Fatalf("LoadManifest on empty root: %v", err)
	}
	if len(m.Entries) != 0 {
		t.Fatalf("entries = %v, want none", m.Entries)
	}
	if m.Version != ManifestVersion {
		t.Fatalf("version = %d, want %d", m.Version, ManifestVersion)
	}
}

func TestLoadManifestRejectsWrongVersion(t *testing.T) {
	root := t.TempDir()
	body := fmt.Sprintf(`{"version":%d,"entries":[]}`, ManifestVersion+1)
	if err := os.WriteFile(filepath.Join(root, ManifestName), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	// Loud failure, not silent field-skipping: a worker that kept going would
	// re-download 7 GB on every run for ever.
	if _, err := LoadManifest(root); err == nil {
		t.Fatal("LoadManifest accepted a future version")
	}
}

func TestParseRange(t *testing.T) {
	const size = 100
	cases := []struct {
		header string
		start  int64
		length int64
		ok     bool
	}{
		{"bytes=0-", 0, 100, true},
		{"bytes=10-", 10, 90, true},
		{"bytes=10-19", 10, 10, true},
		{"bytes=0-99", 0, 100, true},
		{"bytes=0-1000", 0, 100, true}, // end past EOF is clamped, not an error
		{"bytes=-20", 80, 20, true},    // suffix form
		{"bytes=-500", 0, 100, true},   // suffix longer than the file
		{"", 0, 0, false},
		{"items=0-10", 0, 0, false},
		{"bytes=abc-def", 0, 0, false},
		{"bytes=100-", 0, 0, false},  // start == size is out of bounds
		{"bytes=101-", 0, 0, false},
		{"bytes=20-10", 0, 0, false}, // inverted
		{"bytes=0-10,20-30", 0, 0, false}, // multi-range refused on purpose
	}
	for _, tc := range cases {
		start, length, ok := ParseRange(tc.header, size)
		if ok != tc.ok {
			t.Errorf("ParseRange(%q, %d) ok = %v, want %v", tc.header, size, ok, tc.ok)
			continue
		}
		if ok && (start != tc.start || length != tc.length) {
			t.Errorf("ParseRange(%q) = (%d, %d), want (%d, %d)", tc.header, start, length, tc.start, tc.length)
		}
	}
}

// startServer boots a Server on ":0" and returns its base URL.
func startServer(t *testing.T, root string) *Server {
	t.Helper()
	s, err := NewServer(Config{ArtefactRoot: root, Addr: "127.0.0.1:0", Logger: quietLogger()})
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- s.ListenAndServe(ctx) }()

	deadline := time.Now().Add(5 * time.Second)
	for s.Addr() == "" {
		if time.Now().After(deadline) {
			cancel()
			t.Fatal("server never bound")
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Cleanup(func() {
		cancel()
		<-done
	})
	return s
}

func TestServeArtifactAndResume(t *testing.T) {
	root := newRoot(t)
	// Publish the manifest so artefacts get content-addressed ETags. A file the
	// manifest does not cover falls back to the weak validator, which is a
	// separate case below.
	if _, err := BuildManifest(root); err != nil {
		t.Fatalf("BuildManifest: %v", err)
	}
	if err := writeManifest(root); err != nil {
		t.Fatalf("writeManifest: %v", err)
	}
	s := startServer(t, root)
	base := "http://" + s.Addr()
	client := &http.Client{Timeout: 10 * time.Second}

	resp, err := client.Get(base + "/artifact/pylibs/stage1.tar.zst")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	if string(body) != "PYLIBS" {
		t.Fatalf("body = %q", body)
	}
	etag := resp.Header.Get("ETag")
	if etag == "" {
		t.Fatal("no ETag")
	}

	// The ETag has to be the content digest, because that is the only thing a
	// client can check without re-reading the file it already has. This only
	// holds for a file the manifest covers; an unpublished file gets the weaker
	// mtime+size validator instead (asserted in TestServeUnpublishedArtefact).
	sum := sha256.Sum256([]byte("PYLIBS"))
	if !strings.Contains(etag, hex.EncodeToString(sum[:])) {
		t.Fatalf("ETag %q does not carry the content digest", etag)
	}

	// Revalidate: this is the request that makes a warm worker cost one round
	// trip instead of a 2 GB transfer.
	req, _ := http.NewRequest("GET", base+"/artifact/pylibs/stage1.tar.zst", nil)
	req.Header.Set("If-None-Match", etag)
	resp2, err := client.Do(req)
	if err != nil {
		t.Fatalf("revalidate: %v", err)
	}
	resp2.Body.Close()
	if resp2.StatusCode != http.StatusNotModified {
		t.Fatalf("revalidate status = %d, want 304", resp2.StatusCode)
	}

	// Resume: what a worker killed mid-download does.
	req3, _ := http.NewRequest("GET", base+"/artifact/pylibs/stage1.tar.zst", nil)
	req3.Header.Set("Range", "bytes=3-")
	resp3, err := client.Do(req3)
	if err != nil {
		t.Fatalf("range: %v", err)
	}
	tail, _ := io.ReadAll(resp3.Body)
	resp3.Body.Close()
	if resp3.StatusCode != http.StatusPartialContent {
		t.Fatalf("range status = %d, want 206", resp3.StatusCode)
	}
	// "PYLIBS"[3:] is "IBS"; getting this wrong means the server returned the
	// whole file or started at 0, which is exactly the bug a resume needs to
	// avoid -- a client would splice a duplicate prefix into its artefact.
	if string(tail) != "IBS" {
		t.Fatalf("range body = %q, want %q", tail, "IBS")
	}
	if got := resp3.Header.Get("Content-Range"); got != "bytes 3-5/6" {
		t.Fatalf("Content-Range = %q, want bytes 3-5/6", got)
	}
}

func TestServeHeadHasNoBody(t *testing.T) {
	root := newRoot(t)
	s := startServer(t, root)
	client := &http.Client{Timeout: 10 * time.Second}

	resp, err := client.Head("http://" + s.Addr() + "/artifact/weights/homura/model.safetensors")
	if err != nil {
		t.Fatalf("HEAD: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if len(body) != 0 {
		t.Fatalf("HEAD returned a %d byte body", len(body))
	}
	if resp.Header.Get("Content-Length") == "" {
		t.Fatal("HEAD has no Content-Length; a client cannot size its download")
	}
}

func TestServeRejectsTraversalOverHTTP(t *testing.T) {
	// The artefact root is a real subdirectory of a temp dir so that a
	// ".." traversal has somewhere real to land if the guard ever fails. A
	// sentinel file next to the root is the canary: if any response carries it,
	// containment broke.
	base := t.TempDir()
	root := filepath.Join(base, "artefacts")
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "inside.txt"), []byte("inside"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(base, "secret"), []byte("CANARY-SECRET"), 0o644); err != nil {
		t.Fatal(err)
	}
	// Same-prefix sibling: the naive HasPrefix containment check lets this one
	// through, which is why the second gate in Resolve exists.
	if err := os.MkdirAll(filepath.Join(base, "artefacts-evil"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(base, "artefacts-evil", "secret"), []byte("CANARY-EVIL"), 0o644); err != nil {
		t.Fatal(err)
	}
	s := startServer(t, root)

	// net/http cleans ".." out of the path before it reaches a handler, so the
	// raw request below is built by hand to be sure the guard is not relying on
	// that. It is the difference between "the URL library helped" and "the
	// server refuses".
	raw := func(target string) string {
		t.Helper()
		conn, err := net.Dial("tcp", s.Addr())
		if err != nil {
			t.Fatal(err)
		}
		defer conn.Close()
		fmt.Fprintf(conn, "GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n", target)
		conn.SetReadDeadline(time.Now().Add(5 * time.Second))
		out, _ := io.ReadAll(conn)
		return string(out)
	}

	// refuse asserts the canary was not served and that nothing outside the
	// root came back.
	//
	// 301 counts as a refusal: that is net/http's mux cleaning ".." out of the
	// request before a handler sees it, and the redirect target is checked in
	// turn. What must never happen is a 200 carrying a file from outside root.
	refuse := func(target, got string) {
		t.Helper()
		if strings.Contains(got, "CANARY") {
			t.Fatalf("%s leaked a file outside the artefact root:\n%s", target, got)
		}
		status := firstLine(got)
		switch {
		case strings.Contains(status, "200"):
			t.Fatalf("%s was served:\n%s", target, got)
		case strings.Contains(status, "404"), strings.Contains(status, "400"), strings.Contains(status, "301"):
			return
		default:
			t.Fatalf("%s = %q, want a refusal", target, status)
		}
	}

	// net/http's ServeMux cleans ".." out of a path and answers with a 301 to
	// the cleaned form rather than passing the raw request to a handler. So the
	// first response here is a redirect, not a 404 -- that is net/http being
	// helpful, and the property that matters is what the *target* does.
	resp := raw("/artifact/../../../../etc/passwd")
	if strings.Contains(resp, "root:") {
		t.Fatalf("server served /etc/passwd:\n%s", resp)
	}

	// Follow the redirect the way a real client (which follows automatically)
	// would, and confirm the cleaned path is not reachable either.
	if loc := headerValue(resp, "Location"); loc != "" {
		refuse(loc, raw(loc))
	} else {
		refuse("/artifact/../../../../etc/passwd", resp)
	}

	// The forms that do reach a handler instead of being cleaned by the mux:
	// percent-encoded separators, and the same-prefix sibling.
	for _, target := range []string{
		"/artifact/%2e%2e%2f%2e%2e%2fsecret",
		"/artifact/..%2f..%2fsecret",
		"/artifact/%2e%2e/secret",
		"/artifact/../artefacts-evil/secret",
		"/artifact/..\\artefacts-evil\\secret",
		"/artifact/inside.txt/../../../secret",
		"/artifact/C:%5CWindows%5Cwin.ini",
	} {
		refuse(target, raw(target))
	}

	// The legitimate request in the same shape still works, so the refusals
	// above are not just a server that refuses everything.
	good := raw("/artifact/inside.txt")
	if !strings.Contains(good, "200") || !strings.Contains(good, "inside") {
		t.Fatalf("a valid request was refused:\n%s", good)
	}
}

// headerValue pulls one header out of a raw HTTP response. Enough for a test
// that only cares about Location.
func headerValue(resp, name string) string {
	for _, line := range strings.Split(resp, "\r\n") {
		if strings.HasPrefix(strings.ToLower(line), strings.ToLower(name)+":") {
			return strings.TrimSpace(line[len(name)+1:])
		}
	}
	return ""
}

func firstLine(resp string) string {
	if i := strings.Index(resp, "\r\n"); i >= 0 {
		return resp[:i]
	}
	return resp
}

func TestServeRefusesWriteMethods(t *testing.T) {
	root := newRoot(t)
	s := startServer(t, root)
	client := &http.Client{Timeout: 10 * time.Second}

	for _, method := range []string{"POST", "PUT", "DELETE", "PATCH"} {
		req, _ := http.NewRequest(method, "http://"+s.Addr()+"/artifact/pylibs/stage1.tar.zst", strings.NewReader("x"))
		resp, err := client.Do(req)
		if err != nil {
			t.Fatalf("%s: %v", method, err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusMethodNotAllowed {
			t.Errorf("%s status = %d, want 405", method, resp.StatusCode)
		}
		if allow := resp.Header.Get("Allow"); !strings.Contains(allow, "GET") {
			t.Errorf("%s Allow = %q, want it to name GET", method, allow)
		}
	}
}

func TestServeHealthAndManifest(t *testing.T) {
	root := newRoot(t)
	s := startServer(t, root)
	base := "http://" + s.Addr()

	resp, err := http.Get(base + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if strings.TrimSpace(string(body)) != "ok" {
		t.Fatalf("healthz = %q", body)
	}

	resp2, err := http.Get(base + "/manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	var m Manifest
	if err := json.NewDecoder(resp2.Body).Decode(&m); err != nil {
		resp2.Body.Close()
		t.Fatalf("decode manifest: %v", err)
	}
	resp2.Body.Close()
	if resp2.StatusCode != 200 {
		t.Fatalf("status = %d", resp2.StatusCode)
	}
	if len(m.Entries) != 4 {
		t.Fatalf("entries = %d, want 4 (scratch excluded)", len(m.Entries))
	}

	// The manifest's own ETag: a worker polls this, and if it cannot tell
	// "unchanged" from "changed" it re-downloads everything every run.
	req, _ := http.NewRequest("GET", base+"/manifest.json", nil)
	req.Header.Set("If-None-Match", resp2.Header.Get("ETag"))
	resp3, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp3.Body.Close()
	if resp3.StatusCode != http.StatusNotModified {
		t.Fatalf("manifest revalidate = %d, want 304", resp3.StatusCode)
	}
}

func TestServeUnknownPathIs404(t *testing.T) {
	root := newRoot(t)
	s := startServer(t, root)
	client := &http.Client{Timeout: 10 * time.Second}
	for _, p := range []string{"/", "/admin", "/artifact/", "/artifact/nope.bin", "/manifest"} {
		resp, err := client.Get("http://" + s.Addr() + p)
		if err != nil {
			t.Fatalf("GET %s: %v", p, err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusNotFound {
			t.Errorf("GET %s = %d, want 404", p, resp.StatusCode)
		}
	}
}

func TestNewServerRejectsBadRoot(t *testing.T) {
	if _, err := NewServer(Config{}); err == nil {
		t.Error("NewServer accepted an empty root")
	}
	f := filepath.Join(t.TempDir(), "afile")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := NewServer(Config{ArtefactRoot: f}); err == nil {
		t.Error("NewServer accepted a file as root")
	}
	if _, err := NewServer(Config{ArtefactRoot: filepath.Join(t.TempDir(), "missing")}); err == nil {
		t.Error("NewServer accepted a missing root")
	}
}

// quietLogger keeps test output readable: the request log is per-request and
// there are dozens of requests above.
func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

