package edge

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func sha256hex(b []byte) string          { s := sha256.Sum256(b); return hex.EncodeToString(s[:]) }
func jsonNewEncoder(w http.ResponseWriter) *json.Encoder { return json.NewEncoder(w) }

// serve starts a Space over a temp root and returns a client pointed at it.
func serve(t *testing.T, root string) *Client {
	t.Helper()
	if err := writeManifest(root); err != nil {
		t.Fatalf("writeManifest: %v", err)
	}
	s := startServer(t, root)
	return NewClient("http://" + s.Addr())
}

// bigish is a payload big enough to span several copy windows but small enough
// to keep the test fast. 8 MiB window x 3 + a bit.
func bigish(tag string) []byte {
	b := make([]byte, 5<<20)
	for i := range b {
		b[i] = byte(i*31 + len(tag)) // non-constant content, so a misaligned window shows up
	}
	copy(b, tag)
	return b
}

func TestClientPlanMarksCurrentAndMissing(t *testing.T) {
	root := newRoot(t)
	c := serve(t, root)
	ctx := context.Background()

	dest := t.TempDir()
	m, err := c.FetchManifest(ctx)
	if err != nil {
		t.Fatalf("FetchManifest: %v", err)
	}
	p, err := c.PlanAgainst(m, dest)
	if err != nil {
		t.Fatalf("PlanAgainst: %v", err)
	}
	if len(p.Missing) != 4 || len(p.Current) != 0 {
		t.Fatalf("cold plan = %d missing / %d current, want 4/0", len(p.Missing), len(p.Current))
	}

	// Fetch, then re-plan. The second plan must be all-current, or the warm path
	// never engages and the 903 s saving never happens.
	if err := c.Fetch(ctx, dest, p); err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	m2, err := c.FetchManifest(ctx)
	if err != nil {
		t.Fatal(err)
	}
	p2, err := c.PlanAgainst(m2, dest)
	if err != nil {
		t.Fatalf("PlanAgainst (warm): %v", err)
	}
	if len(p2.Missing) != 0 {
		t.Fatalf("warm plan = %d missing, want 0", len(p2.Missing))
	}
	if len(p2.Current) != 4 {
		t.Fatalf("warm plan = %d current, want 4", len(p2.Current))
	}
	if p2.TotalNew != 0 {
		t.Fatalf("warm TotalNew = %d, want 0", p2.TotalNew)
	}
}

func TestClientRefetchesWrongDigest(t *testing.T) {
	root := newRoot(t)
	c := serve(t, root)
	ctx := context.Background()
	dest := t.TempDir()

	m, _ := c.FetchManifest(ctx)
	p, _ := c.PlanAgainst(m, dest)
	if err := c.Fetch(ctx, dest, p); err != nil {
		t.Fatal(err)
	}

	// Corrupt one file in place. Same length, different bytes: the case a size
	// check alone would accept and that would then be unpacked as a bad archive.
	victim := filepath.Join(dest, "pylibs", "stage1.tar.zst")
	if err := os.WriteFile(victim, []byte("corrupt!"), 0o644); err != nil {
		t.Fatal(err)
	}

	m2, _ := c.FetchManifest(ctx)
	p2, _ := c.PlanAgainst(m2, dest)
	if len(p2.Missing) != 1 || p2.Missing[0].Name != "pylibs/stage1.tar.zst" {
		t.Fatalf("missing = %v, want just pylibs/stage1.tar.zst", names(p2))
	}
	if err := c.Fetch(ctx, dest, p2); err != nil {
		t.Fatalf("refetch: %v", err)
	}
	got, err := os.ReadFile(victim)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "PYLIBS" {
		t.Fatalf("after refetch = %q, want PYLIBS", got)
	}
}

func TestClientRefetchesTruncatedFile(t *testing.T) {
	root := newRoot(t)
	c := serve(t, root)
	ctx := context.Background()
	dest := t.TempDir()

	m, _ := c.FetchManifest(ctx)
	p, _ := c.PlanAgainst(m, dest)
	if err := c.Fetch(ctx, dest, p); err != nil {
		t.Fatal(err)
	}

	victim := filepath.Join(dest, "weights", "homura", "model.safetensors")
	if err := os.WriteFile(victim, []byte("short"), 0o644); err != nil {
		t.Fatal(err)
	}
	m2, _ := c.FetchManifest(ctx)
	p2, _ := c.PlanAgainst(m2, dest)
	if len(p2.Missing) != 1 || p2.Missing[0].Name != "weights/homura/model.safetensors" {
		t.Fatalf("missing = %v, want the truncated weights", names(p2))
	}
}

func TestClientLeavesNoPartialFileOnDigestMismatch(t *testing.T) {
	// The important property: a body whose digest does not match must never
	// become visible under its real name, because the next run would find a
	// file of the right size and pack it into the interpreter's path.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/healthz":
			w.Write([]byte("ok"))
		case r.URL.Path == "/"+ManifestName:
			// Publish a manifest claiming a digest of the good body...
			m := &Manifest{Version: ManifestVersion, Entries: []Entry{{
				Name: "pylibs/x.bin", Role: "pylibs", SizeBytes: 5,
				SHA256: "0000000000000000000000000000000000000000000000000000000000000000",
			}}}
			w.Header().Set("Content-Type", "application/json")
			jsonNewEncoder(w).Encode(m)
		case r.URL.Path == "/artifact/pylibs/x.bin":
			// ...then serve different bytes.
			w.Header().Set("Content-Length", "5")
			w.Write([]byte("BAD!!"))
		default:
			w.WriteHeader(404)
		}
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	c.MaxRetries = 1
	dest := t.TempDir()
	ctx := context.Background()

	m, err := c.FetchManifest(ctx)
	if err != nil {
		t.Fatal(err)
	}
	p, err := c.PlanAgainst(m, dest)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Fetch(ctx, dest, p); err == nil {
		t.Fatal("Fetch accepted a body whose digest did not match")
	}

	if _, err := os.Stat(filepath.Join(dest, "pylibs", "x.bin")); !os.IsNotExist(err) {
		t.Fatalf("the bad body was installed anyway: %v", err)
	}
	// And no .part file left behind for the next run to trip over.
	entries, err := os.ReadDir(filepath.Join(dest, "pylibs"))
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".part") {
			t.Fatalf("left a partial file behind: %s", e.Name())
		}
	}
}

func TestClientRetriesTransientFailure(t *testing.T) {
	root := newRoot(t)
	// Publish a manifest, then fail the artefact twice before succeeding.
	if err := writeManifest(root); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	attempts := 0

	inner := http.NewServeMux()
	inner.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
	inner.HandleFunc("/"+ManifestName, func(w http.ResponseWriter, r *http.Request) {
		http.ServeFile(w, r, filepath.Join(root, ManifestName))
	})
	inner.HandleFunc("/artifact/", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		attempts++
		n := attempts
		mu.Unlock()
		if n <= 2 {
			// Hijack and drop the connection: a status code would be a clean
			// failure, this is the messy one a flaky link actually produces.
			hj, ok := w.(http.Hijacker)
			if !ok {
				w.WriteHeader(500)
				return
			}
			conn, _, err := hj.Hijack()
			if err == nil {
				conn.Close()
			}
			return
		}
		http.ServeFile(w, r, filepath.Join(root, strings.TrimPrefix(r.URL.Path, "/artifact/")))
	})
	srv := httptest.NewServer(inner)
	defer srv.Close()

	c := NewClient(srv.URL)
	// Shorten the backoff so the test does not sleep for 1+2 seconds.
	c.MaxRetries = 4
	ctx := context.Background()

	m, err := c.FetchManifest(ctx)
	if err != nil {
		t.Fatalf("FetchManifest: %v", err)
	}
	p, err := c.PlanAgainst(m, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Fetch(ctx, t.TempDir(), p); err != nil {
		t.Fatalf("Fetch did not recover from 2 dropped connections: %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if attempts < 3 {
		t.Fatalf("saw %d attempts, want at least 3", attempts)
	}
}

func TestClientRejectsShortBody(t *testing.T) {
	// The manifest says 5 MiB and the server sends 2 MiB then closes. This must
	// fail rather than install a truncated archive.
	var payload = bigish("SHORT")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/healthz":
			w.Write([]byte("ok"))
		case r.URL.Path == "/"+ManifestName:
			m := &Manifest{Version: ManifestVersion, Entries: []Entry{{
				Name: "weights/big.bin", Role: "weights",
				SizeBytes: int64(len(payload) + 1<<20), // claims more than is sent
				SHA256:  sha256hex(payload),
			}}}
			jsonNewEncoder(w).Encode(m)
		default:
			w.Header().Set("Content-Length", "999999999")
			w.Write(payload)
		}
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	c.MaxRetries = 1
	dest := t.TempDir()
	m, _ := c.FetchManifest(context.Background())
	p, _ := c.PlanAgainst(m, dest)
	err := c.Fetch(context.Background(), dest, p)
	if err == nil {
		t.Fatal("Fetch accepted a short body")
	}
	if !strings.Contains(err.Error(), "bytes") && !strings.Contains(err.Error(), "EOF") {
		t.Logf("error was %v", err)
	}
	if _, err := os.Stat(filepath.Join(dest, "weights", "big.bin")); !os.IsNotExist(err) {
		t.Fatalf("short body was installed: %v", err)
	}
}

func TestClientRejectsOversizedBody(t *testing.T) {
	// The mirror case: server sends more than the manifest claims. Without the
	// bound the file would be inflated and would then fail the digest anyway --
	// but only after writing gigabytes to disk.
	payload := bigish("LONG")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/healthz":
			w.Write([]byte("ok"))
		case r.URL.Path == "/"+ManifestName:
			m := &Manifest{Version: ManifestVersion, Entries: []Entry{{
				Name: "weights/big.bin", Role: "weights",
				SizeBytes: 1024, // claims far less than is sent
				SHA256:  sha256hex(payload),
			}}}
			jsonNewEncoder(w).Encode(m)
		default:
			w.Write(payload)
		}
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	c.MaxRetries = 1
	dest := t.TempDir()
	m, _ := c.FetchManifest(context.Background())
	p, _ := c.PlanAgainst(m, dest)
	if err := c.Fetch(context.Background(), dest, p); err == nil {
		t.Fatal("Fetch accepted more bytes than the manifest declared")
	}
	if _, err := os.Stat(filepath.Join(dest, "weights", "big.bin")); !os.IsNotExist(err) {
		t.Fatalf("oversized body was installed: %v", err)
	}
}

func TestClientFetchesMultiWindowPayload(t *testing.T) {
	// Larger than the 8 MiB copy window, so the loop has to iterate. A window
	// bug would either truncate or duplicate a chunk.
	root := t.TempDir()
	payload := bigish("MULTI")
	full := filepath.Join(root, "weights", "big.bin")
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(full, payload, 0o644); err != nil {
		t.Fatal(err)
	}
	c := serve(t, root)
	dest := t.TempDir()
	ctx := context.Background()

	m, err := c.FetchManifest(ctx)
	if err != nil {
		t.Fatal(err)
	}
	p, err := c.PlanAgainst(m, dest)
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Missing) != 1 {
		t.Fatalf("missing = %d, want 1", len(p.Missing))
	}
	if err := c.Fetch(ctx, dest, p); err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(dest, "weights", "big.bin"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("payload round trip differs: got %d bytes, want %d", len(got), len(payload))
	}
}

func TestClientUnpublishedArtefactIsNotFound(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/healthz":
			w.Write([]byte("ok"))
		case r.URL.Path == "/"+ManifestName:
			m := &Manifest{Version: ManifestVersion, Entries: []Entry{{
				Name: "pylibs/gone.bin", SizeBytes: 3, SHA256: sha256hex([]byte("abc")),
			}}}
			jsonNewEncoder(w).Encode(m)
		default:
			w.WriteHeader(404)
		}
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	c.MaxRetries = 1
	dest := t.TempDir()
	ctx := context.Background()
	m, err := c.FetchManifest(ctx)
	if err != nil {
		t.Fatal(err)
	}
	p, _ := c.PlanAgainst(m, dest)
	// A 404 is not retried, so this returns promptly and says "not published" --
	// which is the signal for the caller to fall back to origin.
	err = c.Fetch(ctx, dest, p)
	if err == nil {
		t.Fatal("Fetch succeeded for an unpublished artefact")
	}
	if !strings.Contains(err.Error(), "not published") {
		t.Fatalf("err = %v, want it to name the artefact as unpublished", err)
	}
}

func TestClientEmptySpaceManifestIsNotAnError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/healthz":
			w.Write([]byte("ok"))
		default:
			w.WriteHeader(404)
		}
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	m, err := c.FetchManifest(context.Background())
	if err != nil {
		t.Fatalf("a Space that published nothing should not be an error: %v", err)
	}
	if len(m.Entries) != 0 {
		t.Fatalf("entries = %v, want none", m.Entries)
	}
	// And a plan over it is empty, so the caller falls back without fetching.
	p, err := c.PlanAgainst(m, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Missing) != 0 {
		t.Fatalf("missing = %v, want none", names(p))
	}
}

func TestClientRespectsContextCancel(t *testing.T) {
	root := newRoot(t)
	// A big payload so the transfer is still running when the context dies.
	payload := bigish("CANCEL")
	if err := os.WriteFile(filepath.Join(root, "weights", "big.bin"), payload, 0o644); err != nil {
		t.Fatal(err)
	}
	c := serve(t, root)
	dest := t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())

	m, _ := c.FetchManifest(ctx)
	p, _ := c.PlanAgainst(m, dest)
	cancel() // before the transfer starts
	if err := c.Fetch(ctx, dest, p); err == nil {
		t.Fatal("Fetch ran despite a cancelled context")
	}
}

func TestClientPing(t *testing.T) {
	root := newRoot(t)
	s := startServer(t, root)
	c := NewClient("http://" + s.Addr())
	if err := c.Ping(context.Background()); err != nil {
		t.Fatalf("Ping: %v", err)
	}

	dead := NewClient("http://127.0.0.1:1")
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := dead.Ping(ctx); err == nil {
		t.Fatal("Ping succeeded against a dead address")
	}
}

// A public Hugging Face repository serves manifest.json under /resolve/main/
// and has no /healthz route at all -- it answers 404 with an HTML error page.
//
// Treating that as "unreachable" would disable the cache on every run while
// printing an error that reads like a network fault. The origin is reachable;
// it simply does not run this service.
func TestClientPingAcceptsOriginWithoutHealthz(t *testing.T) {
	mux := http.NewServeMux()
	// Only the artefact routes exist, exactly as a static file host would have.
	mux.HandleFunc("/manifest.json", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"version":1,"entries":[]}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	c := NewClient(srv.URL)
	if err := c.Ping(context.Background()); err != nil {
		t.Fatalf("Ping against a reachable origin with no /healthz: %v", err)
	}
	if _, err := c.FetchManifest(context.Background()); err != nil {
		t.Fatalf("FetchManifest against the same origin: %v", err)
	}
}

func TestClientContentLength(t *testing.T) {
	root := newRoot(t)
	c := serve(t, root)
	n, err := c.ContentLengthOf(context.Background(), "pylibs/stage1.tar.zst")
	if err != nil {
		t.Fatalf("ContentLengthOf: %v", err)
	}
	if n != int64(len("PYLIBS")) {
		t.Fatalf("length = %d, want %d", n, len("PYLIBS"))
	}
}

func TestClientSkipsUnplaceableManifestEntry(t *testing.T) {
	// One manifest entry names a traversal path. It must be dropped from the
	// plan, not fetched, and not allowed to take the other entries down with it.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/healthz":
			w.Write([]byte("ok"))
		case r.URL.Path == "/"+ManifestName:
			m := &Manifest{Version: ManifestVersion, Entries: []Entry{
				{Name: "../escape.bin", SizeBytes: 3, SHA256: sha256hex([]byte("bad"))},
				{Name: "pylibs/fine.bin", SizeBytes: 3, SHA256: sha256hex([]byte("ok!"))},
			}}
			jsonNewEncoder(w).Encode(m)
		case r.URL.Path == "/artifact/pylibs/fine.bin":
			w.Write([]byte("ok!"))
		default:
			w.WriteHeader(404)
		}
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	dest := t.TempDir()
	ctx := context.Background()
	m, err := c.FetchManifest(ctx)
	if err != nil {
		t.Fatal(err)
	}
	p, err := c.PlanAgainst(m, dest)
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Missing) != 1 || p.Missing[0].Name != "pylibs/fine.bin" {
		t.Fatalf("plan = %v, want only the fine entry", names(p))
	}
	if err := c.Fetch(ctx, dest, p); err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	// Nothing escaped the destination.
	parent := filepath.Dir(dest)
	if _, err := os.Stat(filepath.Join(parent, "escape.bin")); !os.IsNotExist(err) {
		t.Fatalf("a manifest entry escaped the destination: %v", err)
	}
}

func TestClientCreatesNestedDirectories(t *testing.T) {
	root := newRoot(t)
	c := serve(t, root)
	dest := filepath.Join(t.TempDir(), "does", "not", "exist", "yet")
	ctx := context.Background()
	m, _ := c.FetchManifest(ctx)
	p, _ := c.PlanAgainst(m, dest)
	if err := c.Fetch(ctx, dest, p); err != nil {
		t.Fatalf("Fetch: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dest, "weights", "homura", "model.safetensors")); err != nil {
		t.Fatalf("nested artefact missing: %v", err)
	}
}

func TestClientReplacesExistingTargetOnRename(t *testing.T) {
	// Windows refuses to rename over an existing file, so the replacement path
	// has to remove first. This is the case where a same-size corrupt file is
	// present: it must be replaced, not left.
	root := newRoot(t)
	c := serve(t, root)
	dest := t.TempDir()
	ctx := context.Background()

	victim := filepath.Join(dest, "pylibs", "stage1.tar.zst")
	if err := os.MkdirAll(filepath.Dir(victim), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(victim, []byte("XXXXX"), 0o644); err != nil {
		t.Fatal(err)
	}

	m, _ := c.FetchManifest(ctx)
	p, _ := c.PlanAgainst(m, dest)
	// The destination is cold apart from the planted corrupt file, so the plan is
	// everything except... nothing. What matters is that the corrupt file is in
	// it: a same-size file with the wrong bytes must be treated as missing.
	if !contains(p, "pylibs/stage1.tar.zst") {
		t.Fatalf("missing = %v, want the corrupt file among them", names(p))
	}
	if err := c.Fetch(ctx, dest, p); err != nil {
		t.Fatalf("replacing an existing file failed: %v", err)
	}
	got, _ := os.ReadFile(victim)
	if string(got) != "PYLIBS" {
		t.Fatalf("replaced content = %q, want PYLIBS", got)
	}
}

func names(p *Plan) []string {
	out := make([]string, 0, len(p.Missing))
	for _, e := range p.Missing {
		out = append(out, e.Name)
	}
	return out
}

func contains(p *Plan, name string) bool {
	for _, e := range p.Missing {
		if e.Name == name {
			return true
		}
	}
	return false
}