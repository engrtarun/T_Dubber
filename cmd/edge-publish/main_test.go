package main

// The publisher and the fetcher are separate binaries that agree on one JSON
// document. Nothing at build time checks that they still agree, so these tests
// pin the two things that actually break in practice:
//
//  1. the digest written into manifest.json equals the sha256 of the bytes a
//     worker downloads -- if it does not, every publish is rejected at fetch
//     time and the cache silently never engages;
//  2. the manifest the publisher writes parses as the type the client reads.
//
// The second one is why the types are duplicated in main.go rather than
// imported: importing edge/ would compile against its struct tags and could not
// catch a field rename on the wire, whereas decoding here goes through the
// client's own Unmarshal and would.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/engrtarun/tgup/edge"
)

func fakePylibs(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	dir := filepath.Join(root, "pylibs")
	if err := os.MkdirAll(filepath.Join(dir, "vllm"), 0o755); err != nil {
		t.Fatal(err)
	}
	// find_pylibs() requires BOTH of these before it will use a mounted tree,
	// so a publisher that omits either produces a tree that is fetched,
	// unpacked and then ignored.
	if err := os.WriteFile(filepath.Join(dir, ".tdubber_ready"), []byte("ok\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "vllm", "__init__.py"), []byte("x = 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "vllm", "_C.so"), []byte("\x7fELF"), 0o755); err != nil {
		t.Fatal(err)
	}
	return dir
}

// The digest must cover the whole file the client downloads, headers and
// trailing zero blocks included. Hashing only the payloads would pass every
// structural test here and still be rejected by edge-fetch.
func TestPublishedDigestMatchesTheBytesOnDisk(t *testing.T) {
	src := fakePylibs(t)
	stage := t.TempDir()

	entry, err := packageRole(source{role: "pylibs", src: src, prefix: "pylibs"}, stage)
	if err != nil {
		t.Fatalf("packageRole: %v", err)
	}

	archive := filepath.Join(stage, "artifact", "pylibs", "pylibs.tar")
	sum, err := edge.FileSHA256(archive)
	if err != nil {
		t.Fatalf("hashing the published archive: %v", err)
	}
	if sum != entry.SHA256 {
		t.Errorf("manifest digest %s does not match the archive's %s", entry.SHA256, sum)
	}
	info, err := os.Stat(archive)
	if err != nil {
		t.Fatal(err)
	}
	if info.Size() != entry.SizeBytes {
		t.Errorf("manifest size %d, archive is %d bytes", entry.SizeBytes, info.Size())
	}
	if entry.Name != "pylibs/pylibs.tar" {
		t.Errorf("entry name = %q, want pylibs/pylibs.tar", entry.Name)
	}
	if entry.Role != "pylibs" {
		t.Errorf("entry role = %q, want pylibs", entry.Role)
	}
}

// Same bytes, same digest: a worker that already has the artefact must be able
// to decide it is current, and that decision is a digest comparison. Directory
// read order leaking into the archive would change it every run.
func TestPublishIsReproducible(t *testing.T) {
	src := fakePylibs(t)
	first, err := packageRole(source{role: "pylibs", src: src, prefix: "pylibs"}, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(1100 * time.Millisecond) // mtime must differ between the two
	second, err := packageRole(source{role: "pylibs", src: src, prefix: "pylibs"}, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if first.SHA256 != second.SHA256 {
		t.Errorf("digest changed between runs: %s vs %s", first.SHA256, second.SHA256)
	}
}

// What the publisher writes must be readable as the type the client unmarshals.
func TestManifestParsesAsTheClientType(t *testing.T) {
	stage := t.TempDir()
	m := edgeManifest{
		Version:   manifestVersion,
		Generated: "2026-01-01T00:00:00Z",
		Entries: []edgeEntry{{
			Name: "pylibs/pylibs.tar", Role: "pylibs",
			SizeBytes: 1, SHA256: "abc", Modified: "2026-01-01T00:00:00Z",
		}},
		TotalBytes: 1,
	}
	path := filepath.Join(stage, "manifest.json")
	if err := writeManifest(path, &m); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var decoded edge.Manifest
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatalf("the client's Manifest cannot parse what was published: %v", err)
	}
	if decoded.Version != m.Version {
		t.Errorf("version = %d, want %d", decoded.Version, m.Version)
	}
	if len(decoded.Entries) != 1 {
		t.Fatalf("entries = %d, want 1", len(decoded.Entries))
	}
	if decoded.Entries[0].Name != "pylibs/pylibs.tar" ||
		decoded.Entries[0].Role != "pylibs" ||
		decoded.Entries[0].SHA256 != "abc" {
		t.Errorf("entry decoded as %+v", decoded.Entries[0])
	}
	// A version the client would reject must not be publishable by accident.
	if decoded.Version != edge.ManifestVersion {
		t.Errorf("published version %d is not what this client speaks (%d)",
			decoded.Version, edge.ManifestVersion)
	}
}

func TestRefusesATreeFindPylibsWouldIgnore(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "something.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := checkTreeUsable(source{role: "pylibs", src: dir, prefix: "pylibs"}); err == nil {
		t.Error("a tree with no .tdubber_ready must be refused")
	}

	withMarker := fakePylibs(t)
	// Marked ready but missing vllm: find_pylibs() checks for both.
	if err := os.RemoveAll(filepath.Join(withMarker, "vllm")); err != nil {
		t.Fatal(err)
	}
	if err := checkTreeUsable(source{role: "pylibs", src: withMarker, prefix: "pylibs"}); err == nil {
		t.Error("a tree with no vllm/__init__.py must be refused")
	}
}

// The weights role is only required to carry the marker, not vllm.
func TestWeightsRoleNeedsOnlyTheMarker(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, ".tdubber_ready"), []byte("ok"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := checkTreeUsable(source{role: "weights", src: dir, prefix: "hf_cache"}); err != nil {
		t.Errorf("a weights tree with the marker must be accepted: %v", err)
	}
}