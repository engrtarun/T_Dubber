// HTTP transport for the Space.
//
// Four endpoints, all stateless:
//
//	GET  /healthz              liveness, no artefact access
//	GET  /manifest.json        the artefact manifest
//	GET  /artifact/<name>      one artefact, with Range and ETag support
//	GET  /gguf/<file>          one model blob, with its sha256 in a header
//
// WHY A SECOND MODEL ROUTE WHEN /artifact/ ALREADY SERVES BYTES
// -------------------------------------------------------------
// /artifact/ is generic: it serves anything under the root and it has no idea
// what a model is. The client fetching a .gguf has a problem /artifact/ cannot
// answer on its own -- it needs the DIGEST of the blob it is about to download,
// before it commits the transfer, and it needs to know the name it is asking for
// is a model rather than some unrelated file that happens to share the tree.
//
// /gguf/<file> is that lookup, by role instead of by full path:
//
//   - it resolves the name against the roster's own layout, so a model is
//     addressed as a model ("ggml-base.bin") rather than as a path the caller
//     had to reconstruct
//   - it returns X-Content-Sha256, so the client knows what it is verifying
//     against before it spends the bandwidth
//   - it also answers by basename, so a Space that keeps models under weights/
//     (where the old tarball packs put them) serves this route without anyone
//     moving 2 GB of files for the sake of a URL
//
// The bytes are the same bytes /artifact/ would serve and both routes go through
// the same containment check, so this adds addressing, not a second way in.
//
// WHY RANGE AND ETAG MATTER HERE
// -----------------------------
// The whole point of the Space is that a 5 GB weight snapshot and a ~2 GB pylibs
// tree cross the network once and then never again. Both features are load
// bearing:
//
//   - ETag from the content digest: a worker that already has the file sends
//     If-None-Match and gets 304 in a round trip that transfers nothing.
//   - Range: a worker killed mid-download (Kaggle sessions do get reaped) resumes
//     from the byte it reached instead of starting over.
//
// No upload endpoint, no exec, no auth. That is deliberate. This service holds
// files that a worker then unpacks and executes code out of; anything that can
// write here can ship code to every worker. Adding a write path later means
// adding authentication and a signature, and that is a different design with a
// different threat model.

package edge

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
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

// Config is the Space's own settings.
type Config struct {
	// ArtefactRoot is the directory the manifest describes and artifacts are
	// served from. Nothing outside it is reachable: see Resolve.
	ArtefactRoot string

	// Addr is the listen address, e.g. ":8080".
	Addr string

	// Logger receives one line per request. nil means the standard logger.
	Logger *slog.Logger

	// ReadTimeout bounds how long a client may take to send its request.
	ReadTimeout time.Duration
	// WriteTimeout bounds the whole response. It has to exceed the slowest
	// artefact, because a 5 GB body over a slow link is a long response.
	WriteTimeout time.Duration
	// IdleTimeout bounds keep-alive between requests.
	IdleTimeout time.Duration

	// ShutdownGrace is how long in-flight requests get to finish on shutdown.
	ShutdownGrace time.Duration
}

// WithDefaults fills unset durations.
//
// The write timeout is deliberately generous. A Space sits behind a proxy on a
// variable link, and a truncated 5 GB download is worse than a slow one: the
// client would have to detect the short read and resume, which costs another
// round trip to save nothing.
func (c *Config) WithDefaults() {
	if c.Addr == "" {
		c.Addr = ":8080"
	}
	if c.ReadTimeout == 0 {
		c.ReadTimeout = 30 * time.Second
	}
	if c.WriteTimeout == 0 {
		c.WriteTimeout = 2 * time.Hour
	}
	if c.IdleTimeout == 0 {
		c.IdleTimeout = 120 * time.Second
	}
	if c.ShutdownGrace == 0 {
		c.ShutdownGrace = 30 * time.Second
	}
	if c.Logger == nil {
		c.Logger = slog.Default()
	}
}

// Server wraps an http.Server with the Space's routes.
type Server struct {
	cfg  Config
	mux  *http.ServeMux
	http *http.Server
	log  *slog.Logger

	// bound records the address net.Listen actually gave us. It is written once
	// before Serve starts and only read afterwards, and ListenAndServe holds the
	// goroutine open across both, so no mutex is needed. The config's ":0" is
	// useless to a caller that needs to dial the thing.
	boundMu sync.Mutex
	bound   string
}

// NewServer builds the routes. It does not start listening.
func NewServer(cfg Config) (*Server, error) {
	cfg.WithDefaults()
	if cfg.ArtefactRoot == "" {
		return nil, errors.New("edge: ArtefactRoot is required")
	}
	abs, err := filepath.Abs(cfg.ArtefactRoot)
	if err != nil {
		return nil, fmt.Errorf("edge: resolve ArtefactRoot: %w", err)
	}
	info, err := os.Stat(abs)
	if err != nil {
		return nil, fmt.Errorf("edge: ArtefactRoot %s: %w", abs, err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("edge: ArtefactRoot %s is not a directory", abs)
	}
	cfg.ArtefactRoot = abs

	s := &Server{
		cfg:  cfg,
		mux:  http.NewServeMux(),
		log:  cfg.Logger,
		http: &http.Server{
			Addr:              cfg.Addr,
			Handler:           nil, // set below
			ReadTimeout:       cfg.ReadTimeout,
			WriteTimeout:      cfg.WriteTimeout,
			IdleTimeout:       cfg.IdleTimeout,
			ReadHeaderTimeout: 15 * time.Second,
			MaxHeaderBytes:    32 << 10,
		},
	}

	// Explicit 405 plus Allow on every path, instead of relying on the
	// mux's own method check, so the response says which methods exist.
	s.mux.HandleFunc("/healthz", s.requireGET(s.handleHealth))
	s.mux.HandleFunc("/manifest.json", s.requireGET(s.handleManifest))
	s.mux.HandleFunc("/artifact/", s.requireGET(s.handleArtifact))
	s.mux.HandleFunc("/gguf/", s.requireGET(s.handleGguf))

	// Anything else is a 404 from the mux. Deliberately not a redirect to a
	// dashboard: this service has no UI, and inventing one would mean serving
	// pages from a process whose job is to hand over bytes.
	s.http.Handler = s.logging(s.mux)
	return s, nil
}

// requireGET answers a non-GET with 405 and an Allow header.
func (s *Server) requireGET(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.Header().Set("Allow", "GET, HEAD")
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		next(w, r)
	}
}

func (s *Server) logging(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		s.log.Info("request",
			"method", r.Method,
			"path", r.URL.Path,
			"status", rec.status,
			"bytes", rec.written,
			"dur_ms", time.Since(start).Milliseconds(),
		)
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status  int
	written int64
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Write(p []byte) (int, error) {
	n, err := r.ResponseWriter.Write(p)
	r.written += int64(n)
	return n, err
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	// Deliberately does not stat the artefact root. /healthz answers "is this
	// process alive"; a Space restarts it if that is false, and making it depend
	// on a 5 GB directory being readable would turn a transient mount problem
	// into a restart loop.
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	if r.Method == http.MethodHead {
		return
	}
	fmt.Fprintf(w, "ok\n")
}

func (s *Server) handleManifest(w http.ResponseWriter, r *http.Request) {
	m, err := BuildManifest(s.cfg.ArtefactRoot)
	if err != nil {
		s.log.Error("manifest build failed", "err", err)
		http.Error(w, "manifest unavailable", http.StatusInternalServerError)
		return
	}
	body, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		http.Error(w, "manifest unavailable", http.StatusInternalServerError)
		return
	}
	// The manifest changes only when the artefact tree does, and the digest of
	// the manifest is what clients cache against, so it gets its own ETag.
	sum, err := digestOf(body)
	if err != nil {
		http.Error(w, "manifest unavailable", http.StatusInternalServerError)
		return
	}
	if match := r.Header.Get("If-None-Match"); strings.Contains(match, sum) {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	w.Header().Set("ETag", `"`+sum+`"`)
	w.Header().Set("Cache-Control", "public, max-age=60")
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	if r.Method == http.MethodHead {
		return
	}
	_, _ = w.Write(body)
}

func (s *Server) handleArtifact(w http.ResponseWriter, r *http.Request) {
	// r.URL.Path is already percent-decoded by net/http, so a name containing
	// "%2F" arrives as a real separator and would silently walk out of the
	// artefact's own subdirectory. Resolve is the real guard against that; this
	// is just so the name used for lookup and logging is the decoded one.
	name := strings.TrimPrefix(r.URL.Path, "/artifact/")
	if unescaped, err := url.PathUnescape(name); err == nil {
		name = unescaped
	}

	full, err := Resolve(s.cfg.ArtefactRoot, name)
	if err != nil {
		// 404 rather than 400 or 403: whether an artefact exists is not a
		// secret here, and a distinct code would only teach a scanner to
		// distinguish "absent" from "refused".
		http.Error(w, "not found", http.StatusNotFound)
		return
	}

	info, err := os.Stat(full)
	if err != nil || info.IsDir() {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}

	entry, _ := s.findEntry(name)
	s.serveBlob(w, r, name, full, info, entry.SHA256, nil)
}

// handleGguf serves one model blob, addressed by ONE bare filename.
//
// Exactly one path segment, and nothing else. /artifact/ is the route for paths;
// this one is for the question "give me the file called ggml-base.bin", and
// accepting "gguf/ggml-base.bin" or "a/b.gguf" would make the route's answer
// depend on a fallback search -- two spellings, one meaning, and a client that
// guesses wrong gets a 200 from the wrong path or a 404 for a model that is
// sitting right there.
func (s *Server) handleGguf(w http.ResponseWriter, r *http.Request) {
	name := strings.TrimPrefix(r.URL.Path, "/gguf/")
	if unescaped, err := url.PathUnescape(name); err == nil {
		name = unescaped
	}
	name = strings.Trim(name, "/")
	if name == "" || strings.ContainsAny(name, "/\\") {
		http.Error(w, "gguf not published", http.StatusNotFound)
		return
	}

	full, entry, ok := s.resolveGguf(name)
	if !ok {
		http.Error(w, "gguf not published", http.StatusNotFound)
		return
	}
	info, err := os.Stat(full)
	if err != nil || info.IsDir() {
		http.Error(w, "gguf not published", http.StatusNotFound)
		return
	}

	// The digest gets a header of its own as well as serving as the ETag. The
	// header is what the client reads to decide WHAT to verify against before it
	// commits the transfer; the ETag is what it revalidates with afterwards.
	s.serveBlob(w, r, name, full, info, entry.SHA256, map[string]string{
		GgufSHAHeader: entry.SHA256,
	})
}

// resolveGguf maps a bare model filename onto a file under the artefact root.
//
// gguf/<name> is tried first. If it is not there, any manifest entry whose ROLE is
// "gguf" and whose basename matches is used -- that is what lets a Space that
// keeps its models under weights/ (where the old tarball packs put them) answer
// this route without anyone moving 2 GB of files for the sake of a URL. The role
// check is what keeps that fallback from turning the route into a second,
// weaker /artifact/.
//
// Both paths go through Resolve, so a crafted name is contained by exactly the
// same check.
func (s *Server) resolveGguf(name string) (string, Entry, bool) {
	if full, err := Resolve(s.cfg.ArtefactRoot, GgufDirName+"/"+name); err == nil {
		if info, statErr := os.Stat(full); statErr == nil && info.Mode().IsRegular() {
			return full, s.entryForName(name), true
		}
	}

	m, err := LoadManifest(s.cfg.ArtefactRoot)
	if err != nil {
		return "", Entry{}, false
	}
	for _, e := range m.Entries {
		if e.Role != GgufDirName || path.Base(e.Name) != name {
			continue
		}
		full, resolveErr := Resolve(s.cfg.ArtefactRoot, e.Name)
		if resolveErr != nil {
			continue
		}
		return full, e, true
	}
	return "", Entry{}, false
}

// entryForName looks a model up by its canonical name "gguf/<file>", then by
// basename among gguf-role entries. A model that is in no manifest still gets
// served, with the real digest computed from disk, so the client can verify the
// bytes against it and disagree with the roster if it must. An artefact that
// exists but cannot be verified is still better than a model the run has to pull
// 5 GB of from the hub instead, so a weak validator is the fallback rather than a
// 404.
func (s *Server) entryForName(name string) Entry {
	canonical := GgufDirName + "/" + name
	if m, err := LoadManifest(s.cfg.ArtefactRoot); err == nil {
		if e, ok := m.Find(canonical); ok {
			return e
		}
		for _, e := range m.Entries {
			if e.Role == GgufDirName && path.Base(e.Name) == name {
				return e
			}
		}
	}
	sum, err := FileSHA256(path.Join(s.cfg.ArtefactRoot, GgufDirName, name))
	if err != nil {
		return Entry{Name: canonical}
	}
	return Entry{Name: canonical, SHA256: sum}
}

// serveBlob is the shared body path for /artifact/ and /gguf/.
//
// digest may be empty, in which case the weak mtime+size ETag is used and the
// client has no way to verify what it received. That case is stated here rather
// than left to be discovered during an incident.
func (s *Server) serveBlob(w http.ResponseWriter, r *http.Request, name, full string, info os.FileInfo, digest string, extra map[string]string) {
	etag := `"` + digest + `"`
	if digest == "" {
		// Not in the manifest: still serve it, but with a weak validator based
		// on mtime and size so a client can at least revalidate cheaply.
		etag = `"` + weakETag(info.ModTime(), info.Size()) + `"`
	}

	w.Header().Set("ETag", etag)
	w.Header().Set("Accept-Ranges", "bytes")
	// A Space is a public endpoint serving immutable build output, so the
	// client may cache hard. That is what makes the second run cost nothing.
	w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
	// Always say what the content is. The client picks its file name and its
	// loader from this, so a wrong type here becomes a wrong model at load time.
	if ct := contentTypeFor(name); ct != "" {
		w.Header().Set("Content-Type", ct)
	}
	for k, v := range extra {
		if v != "" {
			w.Header().Set(k, v)
		}
	}

	if match := r.Header.Get("If-None-Match"); strings.Contains(match, strings.Trim(etag, `"`)) {
		w.WriteHeader(http.StatusNotModified)
		return
	}

	f, err := os.Open(full)
	if err != nil {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	defer f.Close()

	// start is the seek offset and length is the byte count. Both must be set on
	// the BOTH paths: ParseRange returns 0,0,false for a plain GET, and using that
	// length verbatim would send a zero-byte body behind a correct
	// Content-Length, which a client reads as a silently truncated 5 GB file.
	start, length, ranged := ParseRange(r.Header.Get("Range"), info.Size())
	if !ranged {
		start, length = 0, info.Size()
	}

	// Content-Length must be set before the status line: once WriteHeader has
	// run the header block is gone, and a length that never reaches the client
	// is a body it cannot measure.
	w.Header().Set("Content-Length", strconv.FormatInt(length, 10))
	if ranged {
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, start+length-1, info.Size()))
		w.WriteHeader(http.StatusPartialContent)
	} else {
		w.WriteHeader(http.StatusOK)
	}

	if r.Method == http.MethodHead {
		return
	}
	if start > 0 {
		if _, err := f.Seek(start, io.SeekStart); err != nil {
			// Status is already committed, so this can only be logged. A wrong
			// start would serve the wrong bytes at the right offset, which the
			// client's digest check catches.
			s.log.Error("artifact seek failed", "name", name, "start", start, "err", err)
			return
		}
	}
	if _, err := io.CopyN(w, f, length); err != nil {
		// The status line is already out, so this can only be logged. A short
		// write here is exactly the case Range support exists to recover from.
		s.log.Warn("artifact body truncated", "name", name, "err", err)
	}
}

// contentTypeFor picks a media type from a file name.
//
// application/octet-stream is the default for a model and is correct: the client
// picks its loader from the file name, and a server claiming a .gguf is
// "application/json" would only create a new way to be wrong.
func contentTypeFor(name string) string {
	lower := strings.ToLower(name)
	switch {
	case strings.HasSuffix(lower, ".gguf"), strings.HasSuffix(lower, ".bin"):
		return "application/octet-stream"
	case strings.HasSuffix(lower, ".tar.gz"), strings.HasSuffix(lower, ".tgz"):
		return "application/gzip"
	case strings.HasSuffix(lower, ".zip"):
		return "application/zip"
	default:
		return ""
	}
}

// findEntry looks the name up in the on-disk manifest. A failure to build the
// manifest is not fatal for serving: the file is still readable, it just loses
// its content-addressed ETag this once.
func (s *Server) findEntry(name string) (Entry, bool) {
	m, err := LoadManifest(s.cfg.ArtefactRoot)
	if err != nil {
		return Entry{}, false
	}
	return m.Find(name)
}

func weakETag(mod time.Time, size int64) string {
	return fmt.Sprintf("%d-%d", mod.Unix(), size)
}

func digestOf(body []byte) (string, error) {
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:]), nil
}

// ListenAndServe starts serving and blocks until ctx is cancelled, then drains
// in-flight requests before returning.
//
// The drain matters on a Space: a deploy or a sleep sends SIGTERM, and cutting a
// 5 GB body in half leaves the client with a partial file it has to detect. One
// hour of grace for the slowest artefact is cheaper than the resume.
func (s *Server) ListenAndServe(ctx context.Context) error {
	ln, err := net.Listen("tcp", s.cfg.Addr)
	if err != nil {
		return fmt.Errorf("edge: listen on %s: %w", s.cfg.Addr, err)
	}
	s.boundMu.Lock()
	s.bound = ln.Addr().String()
	s.boundMu.Unlock()
	s.log.Info("listening", "addr", ln.Addr().String(), "root", s.cfg.ArtefactRoot)

	errc := make(chan error, 1)
	go func() {
		err := s.http.Serve(ln)
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		errc <- err
	}()

	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
		s.log.Info("shutting down", "grace", s.cfg.ShutdownGrace)
		shutdownCtx, cancel := context.WithTimeout(context.Background(), s.cfg.ShutdownGrace)
		defer cancel()
		if err := s.http.Shutdown(shutdownCtx); err != nil {
			s.log.Warn("graceful shutdown incomplete", "err", err)
		}
		// Close after Shutdown so a body still streaming gets the full grace.
		if err := s.http.Close(); err != nil {
			s.log.Warn("close reported an error", "err", err)
		}
		return nil
	}
}

// Addr reports the address the listener actually bound to, which is how a test
// that asked for ":0" learns where to dial. Empty before ListenAndServe starts.
func (s *Server) Addr() string {
	s.boundMu.Lock()
	defer s.boundMu.Unlock()
	return s.bound
}
