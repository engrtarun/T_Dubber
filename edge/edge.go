// Package edge is the Hugging Face Space side of T_Dubber.
//
// WHAT THIS IS
// ------------
// An always-on HTTP service that serves the three things a Kaggle worker spends
// 74% of its run waiting for. Measured on run 14 (1220 s total):
//
//	pip install of pylibs            378 s
//	Homura-2B snapshot download      525 s
//	------------------------------------------------
//	setup total                      903 s   (74% of the run)
//
// All three are artefact fetches, not computation. A Space is up 24/7 and has
// no GPU quota, which makes it the right place to hold the bytes.
//
// WHAT IT DELIBERATELY DOES NOT DO
// --------------------------------
// No GPU work. TTS (221 s) and whisper (40 s) stay on Kaggle: a Space has no
// CUDA device, so moving them there would mean not dubbing at all. The win
// here is the 903 s, not the 261 s.
//
// WHY GO
// ------
// The Space is a long-lived process that mostly waits on disk and the network,
// so the runtime is irrelevant to throughput -- what matters is a single static
// binary with no interpreter to keep alive. The repo already ships Go (tgup),
// so this is the same toolchain and the same muscle memory.
//
// The only Python left in the whole path is inside mazinger on the Kaggle GPU
// worker, which is where the models run.

// Package edge implements no protocol of its own: it serves files and accepts
// job descriptions over plain HTTP/JSON, which both ends can speak without a
// dependency.
package edge

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Artifacts are content-addressed by sha256 so a client can verify what it
// downloaded, and so a rebuild that produces identical bytes does not force
// every worker to re-fetch them.
const (
	// ManifestName is the entry point: a client GETs this first, learns which
	// artefacts exist and what their digests are, then fetches only what its
	// own probe says it is missing.
	ManifestName = "manifest.json"

	// ManifestVersion is bumped when the schema below changes shape, so an old
	// client fails loudly instead of misreading a new manifest.
	ManifestVersion = 1
)

// ErrNotFound is returned by resolve for a path outside the artefact root. It
// is deliberately distinct from os.ErrNotExist so a caller can tell "this
// artefact is not published" from "the root is misconfigured".
var ErrNotFound = errors.New("edge: artefact not found")

// FileSHA256 returns the hex digest of a file's contents.
//
// It exists as an exported wrapper because two callers outside this package
// need exactly the digest the manifest is built from: edge-fetch, before it
// unpacks an archive it is about to execute code out of, and the publisher,
// which has to write the same value into manifest.json. Re-deriving it
// anywhere else is how a manifest and the bytes drift apart.
func FileSHA256(path string) (string, error) { return fileSHA256(path) }

// Entry is one published file.
//
// Pylibs and weights are separate entries because they have different lifetimes
// and different reasons to change: pylibs moves when vLLM is re-pinned, weights
// move when the model snapshot moves. Publishing them together would force a
// 5 GB re-download because a pip hash changed.
type Entry struct {
	// Name is the artefact's path relative to the artefact root, always
	// slash-separated regardless of host OS, so a manifest written on Linux
	// resolves on Windows and vice versa.
	Name string `json:"name"`

	// Role lets a client select a subset without hardcoding file names:
	// "pylibs", "weights", "pack".
	Role string `json:"role"`

	SizeBytes int64  `json:"size_bytes"`
	SHA256    string `json:"sha256"`
	Modified  string `json:"modified_utc"`

	// Note is free text for a human reading the manifest. Clients must not
	// parse it.
	Note string `json:"note,omitempty"`
}

// Manifest is the whole published surface.
//
// Version is checked on load so a Space that upgrades its schema mid-run makes
// an old client fail with a clear message rather than silently skipping fields.
type Manifest struct {
	Version    int     `json:"version"`
	Generated  string  `json:"generated_utc"`
	Entries    []Entry `json:"entries"`
	TotalBytes int64   `json:"total_bytes"`
}

// LoadManifest reads manifest.json from root. A missing manifest is returned as
// an empty one rather than an error: a Space that has published nothing yet is a
// valid state, and the client falls back to downloading from origin.
func LoadManifest(root string) (*Manifest, error) {
	path := filepath.Join(root, ManifestName)
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return &Manifest{Version: ManifestVersion}, nil
		}
		return nil, fmt.Errorf("edge: read manifest: %w", err)
	}
	var m Manifest
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("edge: parse manifest: %w", err)
	}
	if m.Version != ManifestVersion {
		return nil, fmt.Errorf("edge: manifest version %d, this build speaks %d",
			m.Version, ManifestVersion)
	}
	if m.Entries == nil {
		m.Entries = []Entry{}
	}
	sort.Slice(m.Entries, func(i, j int) bool { return m.Entries[i].Name < m.Entries[j].Name })
	return &m, nil
}

// BuildManifest walks root and describes every regular file except the manifest
// itself and the scratch directory.
//
// The scratch exclusion matters: the upload spool lives under the same root, and
// folding a 5 GB in-flight file into the manifest would make the manifest change
// on every upload and defeat client caching.
func BuildManifest(root string) (*Manifest, error) {
	m := &Manifest{
		Version:   ManifestVersion,
		Generated: time.Now().UTC().Format(time.RFC3339),
		Entries:   []Entry{},
	}
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			switch d.Name() {
			case ScratchDirName, ".git":
				return filepath.SkipDir
			}
			return nil
		}
		if d.Name() == ManifestName {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return nil
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		sum, err := fileSHA256(path)
		if err != nil {
			return err
		}
		m.Entries = append(m.Entries, Entry{
			Name:       filepath.ToSlash(rel),
			Role:       roleFor(rel),
			SizeBytes:  info.Size(),
			SHA256:     sum,
			Modified:   info.ModTime().UTC().Format(time.RFC3339),
		})
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("edge: walk artefacts: %w", err)
	}
	sort.Slice(m.Entries, func(i, j int) bool { return m.Entries[i].Name < m.Entries[j].Name })
	for _, e := range m.Entries {
		m.TotalBytes += e.SizeBytes
	}
	return m, nil
}

// ScratchDirName is the upload spool. Excluded from the manifest on purpose,
// see BuildManifest.
const ScratchDirName = ".scratch"

// roleFor classifies by path so the client can ask for "weights" without
// hardcoding a filename that will change on the next publish.
func roleFor(rel string) string {
	name := filepath.ToSlash(rel)
	switch {
	case strings.HasPrefix(name, "pylibs/"):
		return "pylibs"
	case strings.HasPrefix(name, "weights/"):
		return "weights"
	case strings.HasPrefix(name, "pack/"):
		return "pack"
	default:
		return "other"
	}
}

// Resolve turns a client-supplied relative path into an absolute path inside
// root, or ErrNotFound.
//
// This is the security boundary of the whole service: the path arrives from the
// network, and os.Root-style containment has to happen before any open. The
// check is done on the CLEANED relative path and then re-verified against the
// joined absolute path, because Clean alone does not stop "../" from escaping
// once it is joined -- Clean("../x") is still "../x".
func Resolve(root, rel string) (string, error) {
	if rel == "" {
		return "", ErrNotFound
	}
	// Normalise the separators a Windows client may send.
	rel = filepath.FromSlash(rel)

	// Reject any leading separator explicitly, not only filepath.IsAbs.
	//
	// On Linux "/etc/passwd" is caught by IsAbs. On Windows it is NOT: there a
	// rooted path is drive-relative, so IsAbs("/etc/passwd") is false and
	// filepath.Join(root, "\etc\passwd") lands *inside* root as "etc\passwd".
	// That is contained and therefore harmless, but it means the same request
	// behaves differently per platform, and a client that gets "etc\passwd"
	// back has no way to know it was not served. Refusing the rooted form on
	// both platforms keeps the contract identical everywhere.
	if strings.HasPrefix(rel, "/") || strings.HasPrefix(rel, `\`) || filepath.IsAbs(rel) {
		return "", ErrNotFound
	}

	// A NUL in the name makes every os call on it fail with EINVAL, which would
	// surface as a confusing error rather than a clean 404. An artefact name
	// never contains one.
	if strings.ContainsRune(rel, 0) {
		return "", ErrNotFound
	}

	// A Windows client may send a drive-relative path like "C:foo". IsAbs is
	// false for it, but it is meaningless against a root we chose, so reject any
	// name whose volume name is set at all.
	if filepath.VolumeName(rel) != "" {
		return "", ErrNotFound
	}

	clean := filepath.Clean(rel)
	if clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return "", ErrNotFound
	}
	if clean == ManifestName {
		return "", ErrNotFound
	}

	absRoot, err := filepath.Abs(root)
	if err != nil {
		return "", err
	}
	full := filepath.Join(absRoot, clean)

	// Second gate: compare against the resolved root rather than trusting the
	// string prefix, so a sibling directory that merely shares a name prefix
	// ("/artefacts-evil" vs "/artefacts") cannot be reached.
	absFull, err := filepath.Abs(full)
	if err != nil {
		return "", err
	}
	if absFull != absRoot && !strings.HasPrefix(absFull, absRoot+string(filepath.Separator)) {
		return "", ErrNotFound
	}
	return absFull, nil
}

// Verify checks a downloaded artefact against the manifest's digest.
//
// Callers must verify before unpacking anything: an artefact is an archive that
// gets extracted into a directory that later runs code out of it, so a
// truncated or substituted file is a code-execution problem and not a wasted
// download.
func (m *Manifest) Verify(name string, r io.Reader) error {
	entry, ok := m.Find(name)
	if !ok {
		return fmt.Errorf("%w: %s", ErrNotFound, name)
	}
	h := sha256.New()
	if _, err := io.Copy(h, r); err != nil {
		return fmt.Errorf("edge: hash %s: %w", name, err)
	}
	got := hex.EncodeToString(h.Sum(nil))
	if !strings.EqualFold(got, entry.SHA256) {
		return fmt.Errorf("edge: %s digest %s, manifest says %s", name, got, entry.SHA256)
	}
	return nil
}

// Find looks an entry up by name, and returns ok=false rather than an error for
// a miss: a client asking for an artefact this Space has not published is a
// normal condition, and it should fall back to the origin.
func (m *Manifest) Find(name string) (Entry, bool) {
	name = filepath.ToSlash(filepath.Clean(filepath.FromSlash(name)))
	for _, e := range m.Entries {
		if e.Name == name {
			return e, true
		}
	}
	return Entry{}, false
}

// ByRole returns every entry with the given role, in name order.
func (m *Manifest) ByRole(role string) []Entry {
	out := []Entry{}
	for _, e := range m.Entries {
		if e.Role == role {
			out = append(out, e)
		}
	}
	return out
}

// PublishManifest builds the manifest for root and writes it to disk.
//
// This is the "publish" step, and it is a whole separate function rather than a
// flag inside the server so that the same code path is what CI runs when it
// stages an artefact and what the Space runs on a cold start. Two code paths
// would drift, and the drift would show up as a digest mismatch at fetch time
// rather than at publish time.
func PublishManifest(root string) error {
	m, err := BuildManifest(root)
	if err != nil {
		return err
	}
	return WriteManifestJSON(filepath.Join(root, ManifestName), m)
}

// WriteManifestJSON marshals m to path.
//
// The write goes to a temporary file in the same directory and is renamed into
// place, because a client polling manifest.json must never read a half-written
// document and conclude the artefact tree is empty.
func WriteManifestJSON(path string, m *Manifest) error {
	return writeManifestTo(path, m)
}

// WriteManifestJSONTo writes the manifest to an arbitrary writer, for --print-manifest
// and for anything that wants it on stdout rather than on disk.
func WriteManifestJSONTo(w io.Writer, m *Manifest) error {
	body, err := marshalManifest(m)
	if err != nil {
		return err
	}
	_, err = w.Write(body)
	return err
}

func marshalManifest(m *Manifest) ([]byte, error) {
	body, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("edge: encode manifest: %w", err)
	}
	return append(body, '\n'), nil
}

func writeManifestTo(path string, m *Manifest) error {
	body, err := marshalManifest(m)
	if err != nil {
		return err
	}
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".manifest-*.tmp")
	if err != nil {
		return fmt.Errorf("edge: temp manifest: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op after a successful rename

	if _, err := tmp.Write(body); err != nil {
		tmp.Close()
		return fmt.Errorf("edge: write manifest: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("edge: close manifest: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		// Windows will not rename over an existing file.
		if rmErr := os.Remove(path); rmErr == nil || os.IsNotExist(rmErr) {
			if err2 := os.Rename(tmpName, path); err2 == nil {
				return nil
			}
		}
		return fmt.Errorf("edge: install manifest: %w", err)
	}
	return nil
}

func fileSHA256(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// ParseRange extracts a single byte range from an HTTP Range header.
//
// Only one range is supported, and only "bytes=" form. A multi-range request is
// refused rather than served partially: clients here fetch whole artefacts, so
// a 206 with an unexpected body would be silently wrong, and returning 416
// costs one retry at worst.
func ParseRange(header string, size int64) (start, length int64, ok bool) {
	if header == "" || size <= 0 {
		return 0, 0, false
	}
	const prefix = "bytes="
	if !strings.HasPrefix(header, prefix) {
		return 0, 0, false
	}
	spec := strings.TrimSpace(strings.TrimPrefix(header, prefix))
	if strings.Contains(spec, ",") {
		return 0, 0, false // multi-range: not supported, on purpose
	}
	dash := strings.Index(spec, "-")
	if dash < 0 {
		return 0, 0, false
	}
	first := strings.TrimSpace(spec[:dash])
	last := strings.TrimSpace(spec[dash+1:])

	if first == "" {
		// Suffix form: the last N bytes. Used to resume a truncated download.
		n, err := strconv.ParseInt(last, 10, 64)
		if err != nil || n <= 0 {
			return 0, 0, false
		}
		if n > size {
			n = size
		}
		return size - n, n, true
	}
	start, err := strconv.ParseInt(first, 10, 64)
	if err != nil || start < 0 || start >= size {
		return 0, 0, false
	}
	length = size - start
	if last != "" {
		end, err := strconv.ParseInt(last, 10, 64)
		if err != nil || end < start {
			return 0, 0, false
		}
		if end >= size {
			end = size - 1
		}
		length = end - start + 1
	}
	return start, length, true
}