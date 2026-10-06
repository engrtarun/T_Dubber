// edge-publish builds the artefacts a Kaggle worker fetches and, optionally,
// pushes them to a public Hugging Face dataset repository.
//
// WHAT THIS IS
// ------------
// The other half of edge-fetch. The worker can only skip its 458 s pip install
// if a verified tree already exists at the origin, and nothing in the previous
// design could put one there: the fetch side was written, the publish side was
// not, so the cache could never have had a first entry.
//
// WHY THE REPOSITORY LAYOUT IS WHAT IT IS
// ---------------------------------------
// The staged tree maps one-to-one onto the repository root:
//
//	manifest.json
//	artifact/<role>/<role>.tar
//
// Because a Hugging Face dataset is served from /resolve/main/, setting
// EDGE_URL to that path makes the repository behave like the file server
// edge/server.go implements -- same manifest.json, same /artifact/<name> -- but
// with no process to run and no $9/month compute bill. The two origins are
// interchangeable by design, not by accident.
//
// WHY UNCOMPRESSED TAR, NOT TAR.GZ
// --------------------------------
// Measured on this project: a 400 MB payload of the kind that actually ships
// here (safetensors and compiled objects are already entropy-coded) compressed
// to a ratio of 1.000, and decompressed at 121 MB/s. For the ~9 GB of pylibs
// and weights a cold run pulls that is:
//
//	tar.gz  ~9.0 GB over the wire  +  74 s of single-threaded decompression
//	tar     ~9.0 GB over the wire  +  0 s
//
// Same bytes on the network, a minute and a quarter of CPU saved, and the
// worker reaches the GPU stage a minute earlier. The compression never bought
// anything: a .whl is a zip and a .safetensors file is float data, so both are
// already at the entropy floor gzip is trying to reach.
//
// WHY THE DIGEST COVERS THE WHOLE ARCHIVE
// ---------------------------------------
// The value in manifest.json has to equal the sha256 of the bytes a worker
// downloads, or edge-fetch rejects the very file this program published. That
// means every header, every payload byte and the trailing zero blocks -- so the
// hash wraps the file itself and archive/tar writes through it, rather than
// hashing the payloads and hoping the framing matches.
//
// WHY PUSHING IS PLAIN GIT
// ------------------------
// git-lfs is how the Hub stores blobs, it is already installed, and it needs no
// Python runtime to reach the Hub. Shelling out to git keeps this tool
// dependency-free and keeps the upload auditable: what lands in the repository
// is exactly what this program wrote into the staging directory.
//
// WHAT IT REFUSES TO PUBLISH
// --------------------------
// A dependency tree without the ready marker, and without vllm importable in
// it. find_pylibs() requires both before it will use a mounted tree, so a tree
// missing either would be published, fetched, unpacked, and then ignored -- a
// silent 458 s of pip behind a log full of "the cache was engaged".

package main

import (
	"archive/tar"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"hash"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// manifestVersion and the entry shape are duplicated from edge/ rather than
// imported, so that a publisher and a client can never disagree about the field
// names at build time. TestManifestShapeMatchesTheClient parses this with the
// client's own types and fails if they drift apart.
const manifestVersion = 1

type edgeManifest struct {
	Version    int         `json:"version"`
	Generated  string      `json:"generated_utc"`
	Entries    []edgeEntry `json:"entries"`
	TotalBytes int64       `json:"total_bytes"`
}

type edgeEntry struct {
	Name      string `json:"name"`
	Role      string `json:"role"`
	SizeBytes int64  `json:"size_bytes"`
	SHA256    string `json:"sha256"`
	Modified  string `json:"modified_utc"`
	Note      string `json:"note,omitempty"`
}

type source struct {
	role   string
	src    string
	prefix string // top-level directory inside the archive
}

// repeatable -add/-as values
type flags []string

func (a *flags) String() string     { return strings.Join(*a, ",") }
func (a *flags) Set(v string) error { *a = append(*a, v); return nil }

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "edge-publish:", err)
		os.Exit(1)
	}
}

func run() error {
	var adds, asFlags flags
	stage := flag.String("stage", "", "staging directory that mirrors the repository root (required)")
	repo := flag.String("repo", "", "Hugging Face dataset repo, e.g. pocotarun/tdubber-edge")
	token := flag.String("token", os.Getenv("HF_TOKEN"), "Hugging Face write token (or set HF_TOKEN)")
	push := flag.Bool("push", false, "git push the staged tree to -repo")
	message := flag.String("message", "", "commit message (default: derived from the entry count)")
	flag.Var(&adds, "add", "role=/path/to/tree to package; repeatable")
	flag.Var(&asFlags, "as", "role=/prefix: the directory name inside the archive (default: the role)")
	flag.Parse()

	if *stage == "" {
		return errors.New("-stage is required")
	}
	if len(adds) == 0 {
		return errors.New("at least one -add role=/path/to/tree is required")
	}

	prefixes := map[string]string{}
	for _, kv := range asFlags {
		role, path, ok := strings.Cut(kv, "=")
		if !ok || strings.TrimSpace(role) == "" || path == "" {
			return fmt.Errorf("-as %q is not role=/prefix", kv)
		}
		prefixes[strings.TrimSpace(role)] = path
	}

	var sources []source
	for _, kv := range adds {
		role, path, ok := strings.Cut(kv, "=")
		role = strings.TrimSpace(role)
		if !ok || role == "" {
			return fmt.Errorf("-add %q is not role=/path/to/tree", kv)
		}
		if strings.ContainsAny(role, `/\:`) {
			return fmt.Errorf("-add %q: the role is a single path segment", kv)
		}
		info, err := os.Stat(path)
		if err != nil {
			return fmt.Errorf("-add %s: %w", role, err)
		}
		if !info.IsDir() {
			return fmt.Errorf("-add %s: %s is not a directory", role, path)
		}
		prefix := role
		if p, ok := prefixes[role]; ok {
			prefix = p
		}
		sources = append(sources, source{role: role, src: path, prefix: prefix})
	}

	if err := os.MkdirAll(filepath.Join(*stage, "artifact"), 0o755); err != nil {
		return err
	}

	manifest := edgeManifest{
		Version:   manifestVersion,
		Generated: time.Now().UTC().Format(time.RFC3339),
	}
	for _, s := range sources {
		if err := checkTreeUsable(s); err != nil {
			return fmt.Errorf("refusing to publish role %q: %w", s.role, err)
		}
		entry, err := packageRole(s, *stage)
		if err != nil {
			return fmt.Errorf("role %q: %w", s.role, err)
		}
		manifest.Entries = append(manifest.Entries, entry)
		manifest.TotalBytes += entry.SizeBytes
		fmt.Printf("  staged %-22s %9.1f MiB  sha256=%s\n",
			entry.Name, float64(entry.SizeBytes)/(1<<20), short(entry.SHA256))
	}

	if err := writeManifest(filepath.Join(*stage, "manifest.json"), &manifest); err != nil {
		return err
	}
	fmt.Printf("  manifest.json: %d entr(y/ies), %.1f MiB total\n",
		len(manifest.Entries), float64(manifest.TotalBytes)/(1<<20))

	if !*push {
		fmt.Printf("\nstaged at %s\n", *stage)
		fmt.Println("Point EDGE_URL at it, or re-run with -push -repo <user>/<dataset> once a write token exists.")
		return nil
	}
	if *repo == "" {
		return errors.New("-push needs -repo <user>/<dataset>")
	}
	if *token == "" {
		return errors.New("-push needs a write token: pass -token or set HF_TOKEN")
	}
	return pushTree(*stage, *repo, *token, *message)
}

// checkTreeUsable enforces exactly what find_pylibs() in the notebook requires
// before it will use a dependency tree.
func checkTreeUsable(s source) error {
	if _, err := os.Stat(filepath.Join(s.src, ".tdubber_ready")); err != nil {
		return errors.New("no .tdubber_ready marker: that file is written only after the install " +
			"and its import probe both passed, and find_pylibs() ignores a tree without it. " +
			"Publish a tree taken from a run that completed.")
	}
	if s.role == "pylibs" {
		if _, err := os.Stat(filepath.Join(s.src, "vllm", "__init__.py")); err != nil {
			return errors.New("no vllm/__init__.py in the tree: find_pylibs() requires it")
		}
	}
	return nil
}

// hashingWriter digests and counts everything written through it.
//
// It wraps the archive file, not the payloads, so the digest covers the tar
// framing as well: headers, padding and the two zero blocks that close the
// file. A digest of the payloads alone would disagree with what edge-fetch
// computes after downloading and every publish would be rejected.
type hashingWriter struct {
	w io.Writer
	h hash.Hash
	n int64
}

func (hw *hashingWriter) Write(p []byte) (int, error) {
	n, err := hw.w.Write(p)
	if n > 0 {
		hw.h.Write(p[:n])
		hw.n += int64(n)
	}
	return n, err
}

// packageRole writes <stage>/artifact/<role>/<role>.tar and returns its entry.
func packageRole(s source, stage string) (edgeEntry, error) {
	outDir := filepath.Join(stage, "artifact", s.role)
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		return edgeEntry{}, err
	}
	outPath := filepath.Join(outDir, s.role+".tar")
	if err := os.Remove(outPath); err != nil && !os.IsNotExist(err) {
		return edgeEntry{}, err
	}

	f, err := os.Create(outPath)
	if err != nil {
		return edgeEntry{}, err
	}
	hw := &hashingWriter{w: f, h: sha256.New()}
	tw := tar.NewWriter(hw)

	if err := writeTree(tw, s); err != nil {
		f.Close()
		os.Remove(outPath)
		return edgeEntry{}, err
	}
	// Close emits the end-of-archive marker through hw, so the digest is
	// complete only afterwards.
	if err := tw.Close(); err != nil {
		f.Close()
		os.Remove(outPath)
		return edgeEntry{}, err
	}
	if err := f.Close(); err != nil {
		os.Remove(outPath)
		return edgeEntry{}, err
	}

	return edgeEntry{
		Name:      filepath.ToSlash(filepath.Join(s.role, s.role+".tar")),
		Role:      s.role,
		SizeBytes: hw.n,
		SHA256:    hex.EncodeToString(hw.h.Sum(nil)),
		Modified:  time.Now().UTC().Format(time.RFC3339),
	}, nil
}

// writeTree streams every file under src into the archive, under prefix.
//
// Sorted traversal makes the archive reproducible: two runs over an unchanged
// tree produce identical bytes and therefore an identical digest, which is what
// lets a worker decide it is already current instead of re-downloading because
// a directory read order differed.
func writeTree(tw *tar.Writer, s source) error {
	type item struct {
		path string
		rel  string
		info os.FileInfo
	}
	var items []item
	if err := filepath.Walk(s.src, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if path == s.src {
			return nil
		}
		rel, err := filepath.Rel(s.src, path)
		if err != nil {
			return err
		}
		items = append(items, item{path: path, rel: rel, info: info})
		return nil
	}); err != nil {
		return err
	}
	sort.Slice(items, func(i, j int) bool { return items[i].rel < items[j].rel })

	for _, it := range items {
		hdr, err := tar.FileInfoHeader(it.info, "")
		if err != nil {
			return err
		}
		hdr.Name = filepath.ToSlash(filepath.Join(s.prefix, it.rel))
		// Drop everything machine-dependent: ownership, device numbers, and
		// access times would otherwise change the digest on every run.
		hdr.Uid, hdr.Gid = 0, 0
		hdr.Uname, hdr.Gname = "", ""
		hdr.AccessTime = time.Time{}
		if err := tw.WriteHeader(hdr); err != nil {
			return err
		}
		if it.info.IsDir() {
			continue
		}
		if !it.info.Mode().IsRegular() {
			// A symlink is a pointer the reader would have to resolve, and a
			// device or fifo is not something a Python tree needs. Refusing is
			// cheaper than finding out later which one was in the archive.
			if it.info.Mode()&os.ModeSymlink != 0 {
				return fmt.Errorf("symlink in source tree: %s (publish a tree without links)", it.rel)
			}
			return fmt.Errorf("irregular file in source tree: %s (%v)", it.rel, it.info.Mode())
		}
		src, err := os.Open(it.path)
		if err != nil {
			return err
		}
		_, err = io.Copy(tw, src)
		src.Close()
		if err != nil {
			return err
		}
	}
	return nil
}

func writeManifest(path string, m *edgeManifest) error {
	buf, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(buf, '\n'), 0o644)
}

// pushTree uploads the staged tree with git and git-lfs.
//
// The token goes into the remote URL rather than into .git/config, so it exists
// nowhere on disk once the command returns: the remote is reset to a token-free
// URL on the way out, pass or fail.
func pushTree(stage, repo, token, message string) error {
	secret := "https://" + repoOwner(repo) + ":" + token + "@huggingface.co/datasets/" + repo
	public := "https://huggingface.co/datasets/" + repo

	if _, err := os.Stat(filepath.Join(stage, ".git")); err != nil {
		if out, err := git(stage, token, "init", "-q"); err != nil {
			return fmt.Errorf("git init: %w: %s", err, out)
		}
	}
	if _, err := git(stage, token, "lfs", "track", "*.tar"); err != nil {
		return fmt.Errorf("git lfs track: %w", err)
	}
	if _, err := git(stage, token, "add", "-A"); err != nil {
		return fmt.Errorf("git add: %w", err)
	}

	if message == "" {
		message = fmt.Sprintf("artefacts: %d entr(y/ies), %.1f MiB",
			len(readManifest(stage)), totalBytes(stage))
	}
	if _, err := git(stage, token, "commit", "-q", "-m", message); err != nil {
		// Nothing staged means every digest is unchanged, which is the common
		// case on a re-publish and exactly the case that needs no push.
		fmt.Println("  nothing changed; the repository already matches this staging tree")
		return nil
	}

	// Remove any previous remote first: git errors on re-adding an existing
	// name, and a stale one would still carry an old token.
	git(stage, token, "remote", "remove", "origin")
	if _, err := git(stage, token, "remote", "add", "origin", secret); err != nil {
		return fmt.Errorf("git remote add: %w", err)
	}
	defer git(stage, token, "remote", "set-url", "origin", public)

	if _, err := git(stage, token, "push", "-u", "origin", "HEAD"); err != nil {
		return fmt.Errorf("git push: %w", err)
	}
	fmt.Printf("  pushed to %s\n", public)
	fmt.Printf("  set EDGE_URL=%s/resolve/main\n", public)
	return nil
}

// git runs git in dir and scrubs the token from anything it prints, because git
// echoes remote URLs in some failure messages.
func git(dir, token string, args ...string) (string, error) {
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	text := strings.ReplaceAll(string(out), token, "<token>")
	return text, err
}

func readManifest(stage string) []edgeEntry {
	buf, err := os.ReadFile(filepath.Join(stage, "manifest.json"))
	if err != nil {
		return nil
	}
	var m edgeManifest
	if json.Unmarshal(buf, &m) != nil {
		return nil
	}
	return m.Entries
}

func totalBytes(stage string) float64 {
	var n int64
	for _, e := range readManifest(stage) {
		n += e.SizeBytes
	}
	return float64(n) / (1 << 20)
}

func repoOwner(repo string) string {
	if i := strings.Index(repo, "/"); i > 0 {
		return repo[:i]
	}
	return repo
}

func short(sha string) string {
	if len(sha) <= 16 {
		return sha
	}
	return sha[:16]
}