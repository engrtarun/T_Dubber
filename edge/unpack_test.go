package edge

// The extraction path is where an archive stops being bytes and becomes code
// that a later run executes, so these tests are about containment first and
// convenience second: a member that escapes must be refused, a refused member
// must not take the rest of the archive with it, and a tree that was not
// finished must never be visible under its real name.

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

type tarEntry struct {
	name    string
	body    string
	mode    int64
	symlink string // when set, the entry is a symlink to this target
	dir     bool
}

func writeTarGz(t *testing.T, path string, entries []tarEntry) {
	t.Helper()
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	gz := gzip.NewWriter(f)
	tw := tar.NewWriter(gz)
	for _, e := range entries {
		hdr := &tar.Header{Name: e.name, Mode: e.mode, ModTime: time.Now()}
		switch {
		case e.symlink != "":
			hdr.Typeflag = tar.TypeSymlink
			hdr.Linkname = e.symlink
			hdr.Mode = 0o777
		case e.dir:
			hdr.Typeflag = tar.TypeDir
			hdr.Mode = 0o755
		default:
			hdr.Typeflag = tar.TypeReg
			hdr.Size = int64(len(e.body))
			hdr.Mode = e.mode
		}
		if err := tw.WriteHeader(hdr); err != nil {
			t.Fatal(err)
		}
		if hdr.Typeflag == tar.TypeReg {
			if _, err := tw.Write([]byte(e.body)); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
}

func writeZip(t *testing.T, path string, files map[string]string) {
	t.Helper()
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	zw := zip.NewWriter(f)
	for name, body := range files {
		w, err := zw.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
}

// Unpacks the archive the way the notebook does: staged, then moved into
// place, with the tree readable and the executable bit preserved.
func TestUnpackInstallsTreeWithMarker(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "pylibs.tar.gz")
	writeTarGz(t, src, []tarEntry{
		{name: "pylibs/", dir: true},
		{name: "pylibs/.tdubber_ready", body: "ok\n", mode: 0o644},
		{name: "pylibs/vllm/__init__.py", body: "VERSION='fake'\n", mode: 0o644},
		{name: "pylibs/vllm/_C.so", body: "\x7fELF-not-really", mode: 0o755},
	})

	target := filepath.Join(dir, "work")
	res, err := Unpack(src, target)
	if err != nil {
		t.Fatalf("Unpack: %v", err)
	}
	if res.Files != 3 {
		t.Errorf("Files = %d, want 3", res.Files)
	}
	if res.Skipped != 0 {
		t.Errorf("Skipped = %d, warns=%v", res.Skipped, res.Warns)
	}

	marker := filepath.Join(target, "pylibs", ".tdubber_ready")
	if _, err := os.Stat(marker); err != nil {
		t.Fatalf("marker missing: %v", err)
	}
	if _, err := os.Stat(filepath.Join(target, "pylibs", "vllm", "__init__.py")); err != nil {
		t.Fatalf("package missing: %v", err)
	}

	// The import needs read+exec on the object file; an archive that lost its
	// mode would produce a tree that exists and fails to load.
	//
	// On Windows the exec bit is not representable -- os.Chmod there only
	// toggles read-only -- so the assertion that matters is the read bit
	// everywhere and the exec bit only where the OS can express one. The
	// extraction itself requests the archive's mode unconditionally; this is a
	// property of the platform, not of the code under test.
	info, err := os.Stat(filepath.Join(target, "pylibs", "vllm", "_C.so"))
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm()&0o444 == 0 {
		t.Errorf("_C.so mode = %v, want it readable", info.Mode().Perm())
	}
	if runtime.GOOS != "windows" && info.Mode().Perm()&0o111 == 0 {
		t.Errorf("_C.so mode = %v, want an exec bit", info.Mode().Perm())
	}
}

// A staging directory is an implementation detail that must not survive: it
// would otherwise consume the Kaggle output quota invisibly, and a later run
// would find two half-trees.
func TestUnpackLeavesNoStagingDirectory(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "a.tar.gz")
	writeTarGz(t, src, []tarEntry{{name: "pkg/__init__.py", body: "", mode: 0o644}})

	target := filepath.Join(dir, "work")
	if _, err := Unpack(src, target); err != nil {
		t.Fatalf("Unpack: %v", err)
	}
	entries, err := os.ReadDir(target)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".edge-unpack-") {
			t.Fatalf("staging directory left behind: %s", e.Name())
		}
	}
}

// The case the containment exists for. `../` in a member name is not exotic --
// it is the whole attack -- and the archive must still deliver the members
// that are legitimate.
func TestUnpackRefusesTraversalAndKeepsTheRest(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "evil.tar.gz")
	writeTarGz(t, src, []tarEntry{
		{name: "../escaped.txt", body: "gotcha", mode: 0o644},
		{name: "good/ok.txt", body: "fine", mode: 0o644},
	})

	target := filepath.Join(dir, "work")
	res, err := Unpack(src, target)
	if err != nil {
		t.Fatalf("Unpack: %v", err)
	}
	if res.Skipped != 1 {
		t.Errorf("Skipped = %d, want 1 (warns=%v)", res.Skipped, res.Warns)
	}
	if _, err := os.Stat(filepath.Join(dir, "escaped.txt")); !os.IsNotExist(err) {
		t.Errorf("traversal member escaped: stat err = %v", err)
	}
	if _, err := os.Stat(filepath.Join(target, "good", "ok.txt")); err != nil {
		t.Errorf("legitimate member was lost with the refused one: %v", err)
	}
}

func TestUnpackRefusesAbsoluteMember(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "abs.tar.gz")
	writeTarGz(t, src, []tarEntry{
		{name: "/etc/edge-owned", body: "x", mode: 0o644},
		{name: "pkg/ok.txt", body: "fine", mode: 0o644},
	})

	target := filepath.Join(dir, "work")
	res, err := Unpack(src, target)
	if err != nil {
		t.Fatalf("Unpack: %v", err)
	}
	if res.Skipped != 1 {
		t.Errorf("Skipped = %d, want 1", res.Skipped)
	}
	if _, err := os.Stat(filepath.Join(target, "pkg", "ok.txt")); err != nil {
		t.Errorf("good member missing: %v", err)
	}
}

// A symlink whose target points outside is refused; one that stays inside is
// recreated. This is the member type that can reach out without any of its own
// bytes looking wrong.
func TestUnpackSymlinkContainment(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "links.tar.gz")
	writeTarGz(t, src, []tarEntry{
		{name: "pkg/real.so", body: "elf", mode: 0o644},
		{name: "pkg/alias.so", symlink: "real.so"},
		{name: "pkg/escape.so", symlink: "../../outside.so"},
	})

	target := filepath.Join(dir, "work")
	res, err := Unpack(src, target)
	if err != nil {
		t.Fatalf("Unpack: %v", err)
	}

	// The escaping link is refused on every platform -- that is the security
	// property. Recreating the harmless one is a platform capability: Windows
	// refuses symlink creation without developer mode or a privilege, and a
	// refusal there must be recorded rather than crash the extraction.
	if res.Skipped < 1 {
		t.Errorf("Skipped = %d, want at least the escaping link (warns=%v)", res.Skipped, res.Warns)
	}
	if runtime.GOOS == "windows" {
		if res.Links != 0 {
			t.Errorf("Links = %d on Windows, want 0 (creation is expected to be refused)", res.Links)
		}
	} else {
		if res.Links != 1 {
			t.Errorf("Links = %d, want 1 (warns=%v)", res.Links, res.Warns)
		}
		if res.Skipped != 1 {
			t.Errorf("Skipped = %d, want 1 for the escaping link", res.Skipped)
		}
		if got, err := os.Readlink(filepath.Join(target, "pkg", "alias.so")); err != nil {
			t.Errorf("internal symlink not recreated: %v", err)
		} else if got != "real.so" {
			t.Errorf("symlink target = %q, want real.so", got)
		}
	}

	if _, err := os.Lstat(filepath.Join(target, "pkg", "escape.so")); !os.IsNotExist(err) {
		t.Errorf("escaping symlink was created, stat err = %v", err)
	}
	if _, err := os.Lstat(filepath.Join(dir, "outside.so")); !os.IsNotExist(err) {
		t.Errorf("escaping symlink target materialised outside: err = %v", err)
	}
}

func TestUnpackZip(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "tree.zip")
	writeZip(t, src, map[string]string{
		"pylibs/.tdubber_ready": "ok",
		"pylibs/vllm/__init__.py": "x = 1\n",
	})

	target := filepath.Join(dir, "work")
	res, err := Unpack(src, target)
	if err != nil {
		t.Fatalf("Unpack: %v", err)
	}
	if res.Files != 2 {
		t.Errorf("Files = %d, want 2", res.Files)
	}
	body, err := os.ReadFile(filepath.Join(target, "pylibs", "vllm", "__init__.py"))
	if err != nil || string(body) != "x = 1\n" {
		t.Errorf("body = %q, err = %v", body, err)
	}
}

func TestUnpackUnsupportedFormat(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "weights.bin")
	if err := os.WriteFile(src, []byte("not an archive"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := Unpack(src, filepath.Join(dir, "work")); !errors.Is(err, ErrUnsupportedArchive) {
		t.Errorf("err = %v, want ErrUnsupportedArchive", err)
	}
}

func TestUnpackEmptyArchiveFails(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "empty.tar.gz")
	writeTarGz(t, src, nil)
	if _, err := Unpack(src, filepath.Join(dir, "work")); err == nil {
		t.Error("Unpack of an archive with no members should fail")
	}
}

// A tree left by an earlier run must not survive alongside the new one: a
// stale pylibs with a marker from a previous install is the exact failure the
// staging rename exists to prevent.
func TestUnpackReplacesExistingTree(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "pylibs.tar.gz")
	writeTarGz(t, src, []tarEntry{
		{name: "pylibs/new.txt", body: "new", mode: 0o644},
	})

	target := filepath.Join(dir, "work")
	stale := filepath.Join(target, "pylibs")
	if err := os.MkdirAll(stale, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stale, "old.txt"), []byte("old"), 0o644); err != nil {
		t.Fatal(err)
	}

	if _, err := Unpack(src, target); err != nil {
		t.Fatalf("Unpack: %v", err)
	}
	if _, err := os.Stat(filepath.Join(stale, "old.txt")); !os.IsNotExist(err) {
		t.Errorf("stale file survived, err = %v", err)
	}
	if _, err := os.Stat(filepath.Join(stale, "new.txt")); err != nil {
		t.Errorf("new file missing: %v", err)
	}
}

// Nothing half-written may be visible under the real path, so if reading the
// archive fails halfway, the destination keeps whatever it had.
func TestUnpackCorruptArchiveLeavesDestinationUntouched(t *testing.T) {
	dir := t.TempDir()
	src := filepath.Join(dir, "trunc.tar.gz")
	writeTarGz(t, src, []tarEntry{{name: "pkg/a.txt", body: strings.Repeat("x", 4096), mode: 0o644}})

	raw, err := os.ReadFile(src)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(src, raw[:len(raw)/2], 0o644); err != nil {
		t.Fatal(err)
	}

	target := filepath.Join(dir, "work")
	if _, err := Unpack(src, target); err == nil {
		t.Fatal("Unpack of a truncated archive should fail")
	}
	if _, err := os.Stat(filepath.Join(target, "pkg", "a.txt")); !os.IsNotExist(err) {
		t.Errorf("partial file visible under the real path, err = %v", err)
	}
	entries, err := os.ReadDir(target)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".edge-unpack-") {
			t.Errorf("staging directory left behind after failure: %s", e.Name())
		}
	}
}

func TestIsArchive(t *testing.T) {
	cases := map[string]bool{
		"pylibs/pylibs.tar.gz": true,
		"weights/snap.tgz":     true,
		"weights/snap.tar":     true,
		"pack/bin.zip":         true,
		"README.md":            false,
		"model.safetensors":    false,
		"pylibs.tar.gz.part":   false,
	}
	for name, want := range cases {
		if got := IsArchive(name); got != want {
			t.Errorf("IsArchive(%q) = %v, want %v", name, got, want)
		}
	}
}

// inside() is the predicate every containment decision funnels through, so its
// prefix case is tested directly: /data/artefacts-evil is not in /data/artefacts.
func TestInside(t *testing.T) {
	base := filepath.FromSlash("/data/artefacts")
	cases := []struct {
		path string
		want bool
	}{
		{"/data/artefacts", true},
		{"/data/artefacts/a/b", true},
		{"/data/artefacts-evil", false},
		{"/data/artefacts-evil/a", false},
		{"/data", false},
		{"/etc/passwd", false},
	}
	for _, c := range cases {
		got := inside(base, filepath.FromSlash(c.path))
		if got != c.want {
			t.Errorf("inside(%q) = %v, want %v", c.path, got, c.want)
		}
	}
}

// A memberPath result must be inside dest for every name the archives in this
// repo actually carry.
func TestMemberPathAcceptsNormalNames(t *testing.T) {
	dest := t.TempDir()
	for _, name := range []string{
		"pylibs/vllm/__init__.py",
		"pylibs/.tdubber_ready",
		"hf_cache/models--IndexTeam--Index-Homura-2B/snapshots/abc/config.json",
	} {
		if _, err := memberPath(dest, name); err != nil {
			t.Errorf("memberPath(%q) = %v", name, err)
		}
	}
	for _, name := range []string{"../x", "a/../../x", "/etc/passwd", ""} {
		if _, err := memberPath(dest, name); err == nil {
			t.Errorf("memberPath(%q) accepted a dangerous name", name)
		}
	}
}

// writeFile must produce a readable file even when the archive recorded a mode
// with no read bits, or the installed tree cannot be imported by its owner.
func TestWriteFileRepairsUnreadableMode(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "sub", "f.bin")
	if _, err := writeFile(p, 0o000, bytes.NewReader([]byte("data"))); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(p)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm()&0o400 == 0 {
		t.Errorf("mode = %v, want the owner to be able to read it", info.Mode().Perm())
	}
}
