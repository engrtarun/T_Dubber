package edge

// The /gguf/ route and the "gguf" role: the fetch path the PyTorch-free
// architecture downloads its models through. These tests pin the same guarantees
// the /artifact/ route has -- Range resume, content-digest ETag, containment --
// plus the one thing /gguf/ adds: the digest is also advertised in
// X-Content-Sha256 before the body starts, which is how a client that has no
// digest of its own decides what to verify against.

import (
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const someModelFile = "Index-Homura-2B.Q4_K_M.gguf"

// newGGUFRoot builds a root containing one published .gguf under gguf/ and
// publishes the manifest for it, then returns root and the file's digest.
func newGGUFRoot(t *testing.T) (string, string) {
	t.Helper()
	root := t.TempDir()
	body := []byte("GGUFBYTES-8c2b03de")
	if err := os.MkdirAll(filepath.Join(root, GgufDirName), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, GgufDirName, someModelFile), body, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := writeManifest(root); err != nil {
		t.Fatalf("writeManifest: %v", err)
	}
	sum := sha256.Sum256(body)
	return root, hex.EncodeToString(sum[:])
}

// roleFor is what makes `edge-fetch -role gguf` work without the client knowing
// any file names, so a wrong classification is a silent fetch of the wrong thing.
func TestRoleForClassifiesGGUF(t *testing.T) {
	if got := roleFor(GgufDirName + "/" + someModelFile); got != "gguf" {
		t.Errorf("roleFor(gguf/...) = %q, want gguf", got)
	}
	// A file that merely lives under a directory NAMED gguf but outside the
	// role prefix is an ordinary artefact and must not pick up the role.
	for _, path := range []string{"weights/" + someModelFile, "ggufz/x.bin", "other/x.bin"} {
		if got := roleFor(path); got == "gguf" {
			t.Errorf("roleFor(%q) = gguf, want anything else", path)
		}
	}
}

func TestManifestMarksGGUFEntries(t *testing.T) {
	root, _ := newGGUFRoot(t)
	m, err := BuildManifest(root)
	if err != nil {
		t.Fatalf("BuildManifest: %v", err)
	}
	entry, ok := m.Find(GgufDirName + "/" + someModelFile)
	if !ok {
		t.Fatal("gguf entry missing from manifest")
	}
	if entry.Role != "gguf" {
		t.Errorf("role = %q, want gguf", entry.Role)
	}
	byRole := m.ByRole("gguf")
	if len(byRole) != 1 || byRole[0].Name != GgufDirName+"/"+someModelFile {
		t.Errorf("ByRole(gguf) = %v", byRole)
	}
}

func TestServeGGUFRoute(t *testing.T) {
	root, sum := newGGUFRoot(t)
	s := startServer(t, root)
	base := "http://" + s.Addr()
	client := &http.Client{Timeout: 10 * time.Second}
	url := base + "/gguf/" + someModelFile

	resp, err := client.Get(url)
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	// The digest must appear BEFORE the body: the client reads this header to
	// decide what it is going to verify against, then revalidates with the ETag.
	if got := resp.Header.Get(GgufSHAHeader); got != sum {
		t.Errorf("X-Content-Sha256 = %q, want %q", got, sum)
	}
	if etag := resp.Header.Get("ETag"); !strings.Contains(etag, sum) {
		t.Errorf("ETag %q does not carry the content digest", etag)
	}
	// A model gets its own content type so the loader can pick itself.
	if resp.Header.Get("Content-Type") == "" {
		t.Error("no Content-Type for a gguf")
	}

	// Range resume, the same contract as /artifact/: a session reaped mid-download
	// restarts at the byte it reached.
	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("Range", "bytes=4-")
	resp2, err := client.Do(req)
	if err != nil {
		t.Fatalf("range: %v", err)
	}
	tail, _ := io.ReadAll(resp2.Body)
	resp2.Body.Close()
	if resp2.StatusCode != http.StatusPartialContent {
		t.Fatalf("range status = %d, want 206", resp2.StatusCode)
	}
	if want := string(body[4:]); string(tail) != want {
		t.Errorf("range body = %q, want %q", tail, want)
	}

	// HEAD stays bodyless but keeps the digest header: that is how a cache
	// checks "what is on the Space" without paying for the bytes.
	resp3, err := client.Head(url)
	if err != nil {
		t.Fatalf("HEAD: %v", err)
	}
	resp3.Body.Close()
	if resp3.Header.Get(GgufSHAHeader) != sum {
		t.Errorf("HEAD lost the digest header: %q", resp3.Header.Get(GgufSHAHeader))
	}
}

func TestServeGGUFIsOpaqueByDesign(t *testing.T) {
	root, _ := newGGUFRoot(t)
	s := startServer(t, root)
	client := &http.Client{Timeout: 10 * time.Second}
	base := "http://" + s.Addr()

	// The route is task-shaped: one bare filename. Anything that needs a path
	// belongs to /artifact/.
	for _, p := range []string{
		"/gguf/", "/gguf/../README.txt", "/gguf/" + GgufDirName + "/" + someModelFile,
		"/gguf/missing.bin", "/gguf/a/b.gguf",
	} {
		resp, err := client.Get(base + p)
		if err != nil {
			t.Fatalf("GET %s: %v", p, err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusNotFound {
			t.Errorf("GET %s = %d, want 404", p, resp.StatusCode)
		}
	}

	// The canary: nothing outside the gguf directory may be served through the
	// route even when the name is crafted.
	resp, err := client.Get(base + "/gguf/%2e%2e/README.txt")
	if err != nil {
		t.Fatalf("GET encoded traversal: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("encoded traversal = %d, want 404", resp.StatusCode)
	}
}
