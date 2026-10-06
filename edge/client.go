package edge

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// These three exist so client.go does not need crypto imports spelled inline in
// the middle of transfer logic.
type net_Dialer = net.Dialer

func newSHA256() hash.Hash          { return sha256.New() }
func hexOf(h hash.Hash) string      { return hex.EncodeToString(h.Sum(nil)) }

// Client is the Kaggle side of the Space: it fetches the manifest, works out
// what is missing, and pulls only that.
//
// WHY THE CLIENT IS STRUCTURED AROUND "MISSING", NOT "DOWNLOAD"
// ------------------------------------------------------------
// A worker does not know in advance what it needs. The probe order is:
//
//  1. ask the Space what exists          (one small request)
//  2. compare digests against what is already on local disk
//  3. fetch only the entries that disagree
//
// Step 2 is the entire point. Without it the notebook pays the full 903 s every
// single run; with it a warm worker pays ~8 s of revalidation. This is the same
// mechanism as pip's HTTP cache and the HF hub's blob cache, and it is why the
// cache lives behind a manifest of content digests rather than behind fixed
// filenames.

// Client talks to one Space.
type Client struct {
	BaseURL string
	HTTP    *http.Client
	// MaxRetries bounds the retry of a transient failure. Retrying is safe
	// because a fetch is a pure read and the body is digest-checked.
	MaxRetries int
	// Progress, when non-nil, receives one line per transfer phase.
	Progress func(format string, args ...any)
	// Log, when non-nil, receives one line per notable event.
	Log func(format string, args ...any)
}

// NewClient returns a Client with timeouts suited to multi-gigabyte bodies.
//
// The client-side timeout is deliberately long and separate from the dial
// timeout: a 5 GB artefact on a slow link is minutes of transfer, and a
// uniform 30 s timeout would abort every large fetch. Dial and TLS handshake
// stay short because a hung connect is never legitimate.
func NewClient(baseURL string) *Client {
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		HTTP: &http.Client{
			Transport: &http.Transport{
				DialContext: (&net_Dialer{Timeout: 15 * time.Second}).DialContext,
				TLSHandshakeTimeout:   20 * time.Second,
				ResponseHeaderTimeout: 60 * time.Second,
				ExpectContinueTimeout: 5 * time.Second,
				// Artefacts are large and few; a bounded idle pool keeps a long
				// download from starving a second one.
				MaxIdleConns:        8,
				MaxIdleConnsPerHost: 4,
				IdleConnTimeout:     90 * time.Second,
			},
			Timeout: 0, // bounded by the caller's context instead
		},
		MaxRetries: 4,
	}
}

func (c *Client) progress(format string, args ...any) {
	if c.Progress != nil {
		c.Progress(format, args...)
	}
}

func (c *Client) logf(format string, args ...any) {
	if c.Log != nil {
		c.Log(format, args...)
	}
}

// Ping checks that the origin answers at all.
//
// It exists separately from FetchManifest because the failure modes differ:
// "nothing is listening" is a wait-or-fall-back decision, and knowing it
// before parsing a manifest is what keeps the fallback cheap.
//
// ANY HTTP status counts as answering -- including 404, 405 and 503. The
// origin is frequently not this service: on the free path the artefacts live
// in a public Hugging Face repository served from /resolve/main/, which has
// never heard of /healthz and answers it with a 404. A strict "must be 200"
// check reports such an origin as unreachable and disables the cache on every
// run -- a bug that presents as "the cache never helps", with nothing in the
// log but an "unreachable" line that looks like a network problem.
//
// The manifest remains the authority on whether artefacts exist. Ping only
// answers one question: is anything connected.
func (c *Client) Ping(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/healthz", nil)
	if err != nil {
		return err
	}
	resp, err := c.http().Do(req)
	if err != nil {
		return fmt.Errorf("edge: ping %s: %w", c.BaseURL, err)
	}
	drain(resp)
	return nil
}

func (c *Client) http() *http.Client {
	if c.HTTP != nil {
		return c.HTTP
	}
	return http.DefaultClient
}

// FetchManifest gets the Space's artefact list.
//
// A 404 here is deliberately NOT an error: a Space that has published nothing is
// a valid deployment, and the caller is expected to fall back to origin.
func (c *Client) FetchManifest(ctx context.Context) (*Manifest, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/"+ManifestName, nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.http().Do(req)
	if err != nil {
		return nil, fmt.Errorf("edge: fetch manifest: %w", err)
	}
	defer drain(resp)
	if resp.StatusCode == http.StatusNotFound {
		return &Manifest{Version: ManifestVersion}, nil
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("edge: fetch manifest: status %d", resp.StatusCode)
	}
	var m Manifest
	if err := json.NewDecoder(resp.Body).Decode(&m); err != nil {
		return nil, fmt.Errorf("edge: parse manifest: %w", err)
	}
	if m.Version != ManifestVersion {
		return nil, fmt.Errorf("edge: manifest version %d, this build speaks %d", m.Version, ManifestVersion)
	}
	if m.Entries == nil {
		m.Entries = []Entry{}
	}
	return &m, nil
}

// Plan is what Fetch would do, worked out before anything is transferred.
//
// Separating the plan from the transfer is what makes the notebook's behaviour
// inspectable: it can log "3 of 4 artefacts already current, fetching weights"
// before committing 5 GB to the network, and it can decline to fetch anything at
// all when the answer is "none missing".
type Plan struct {
	Entries  []Entry // everything the Space publishes
	Missing  []Entry // what has to be transferred
	Current  []Entry // already on disk with the right digest
	TotalNew int64   // bytes to transfer
}

// PlanAgainst compares the manifest against what is already in destDir.
//
// The on-disk check is digest-based, not name-based: a file whose name matches
// but whose content does not is treated as missing, because that is the case
// where a half-written download would otherwise be accepted.
func (c *Client) PlanAgainst(m *Manifest, destDir string) (*Plan, error) {
	p := &Plan{Entries: m.Entries}
	for _, e := range m.Entries {
		full, err := Resolve(destDir, e.Name)
		if err != nil {
			// The manifest names a path we cannot place inside the destination.
			// Skipping it is safer than failing the whole plan: one bad entry
			// should not cost the other 5 GB.
			c.logf("skipping manifest entry we cannot place: %s", e.Name)
			continue
		}
		info, err := os.Stat(full)
		switch {
		case err != nil:
			p.Missing = append(p.Missing, e)
		case !info.Mode().IsRegular():
			p.Missing = append(p.Missing, e)
		case info.Size() != e.SizeBytes:
			// Wrong length is cheap to detect and almost always means a
			// truncated transfer, so it avoids hashing gigabytes.
			c.logf("%s: size %d, manifest says %d; refetching", e.Name, info.Size(), e.SizeBytes)
			p.Missing = append(p.Missing, e)
		default:
			// Same length. Only a digest can tell a good file from a substituted
			// one, so hash it -- this is a local read, and it is what makes
			// "already current" mean actually current.
			sum, err := fileSHA256(full)
			if err != nil {
				c.logf("%s: hash failed (%v); refetching", e.Name, err)
				p.Missing = append(p.Missing, e)
				continue
			}
			if !strings.EqualFold(sum, e.SHA256) {
				c.logf("%s: digest %s, manifest says %s; refetching", e.Name, sum[:12], e.SHA256[:12])
				p.Missing = append(p.Missing, e)
				continue
			}
			p.Current = append(p.Current, e)
		}
	}
	for _, e := range p.Missing {
		p.TotalNew += e.SizeBytes
	}
	return p, nil
}

// Fetch downloads everything the plan lists as missing into destDir.
//
// Artefacts are fetched to a temporary name and renamed into place only after
// the digest matches. That ordering is not defensive decoration: the whole point
// of the digest is that a worker must never execute or import a partially
// written file, and a rename is the only portable way to make "complete" and
// "visible" the same instant.
func (c *Client) Fetch(ctx context.Context, destDir string, p *Plan) error {
	if len(p.Missing) == 0 {
		c.progress("edge: nothing to fetch, %d artefact(s) already current", len(p.Current))
		return nil
	}
	total := int64(0)
	for _, e := range p.Missing {
		total += e.SizeBytes
	}
	c.progress("edge: fetching %d artefact(s), %.1f MiB", len(p.Missing), float64(total)/(1<<20))

	var firstErr error
	for _, e := range p.Missing {
		if err := ctx.Err(); err != nil {
			return err
		}
		if err := c.fetchOne(ctx, destDir, e); err != nil {
			// Keep going: pylibs and weights are independent, and a worker can
			// still run the stages whose artefacts arrived. Record the first
			// failure and report it at the end.
			c.logf("edge: %s failed: %v", e.Name, err)
			if firstErr == nil {
				firstErr = err
			}
		}
	}
	return firstErr
}

func (c *Client) fetchOne(ctx context.Context, destDir string, e Entry) error {
	full, err := Resolve(destDir, e.Name)
	if err != nil {
		return fmt.Errorf("edge: destination for %s: %w", e.Name, err)
	}
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		return fmt.Errorf("edge: mkdir for %s: %w", e.Name, err)
	}

	// Same directory as the target, so the rename below stays within one
	// filesystem and is therefore atomic.
	tmp, err := os.CreateTemp(filepath.Dir(full), ".edge-*.part")
	if err != nil {
		return fmt.Errorf("edge: temp file for %s: %w", e.Name, err)
	}
	tmpName := tmp.Name()
	defer func() {
		tmp.Close()
		os.Remove(tmpName) // no-op once the rename succeeded
	}()

	body, err := c.open(ctx, e)
	if err != nil {
		return err
	}
	defer body.Close()

	c.progress("edge: %s (%.1f MiB)", e.Name, float64(e.SizeBytes)/(1<<20))
	h := newSHA256()
	// CopyN is bounded by the manifest size rather than reading to EOF: a
	// server that sends more than it advertised gets truncated here instead of
	// silently inflating the file.
	written, err := copyProgress(tmp, h, body, e.SizeBytes, c.progress)
	if err != nil {
		return fmt.Errorf("edge: transfer %s: %w", e.Name, err)
	}
	if written != e.SizeBytes {
		return fmt.Errorf("edge: %s: got %d bytes, manifest says %d", e.Name, written, e.SizeBytes)
	}
	got := hexOf(h)
	if !strings.EqualFold(got, e.SHA256) {
		// The temp file is removed by the defer, so the bad bytes never become
		// visible under the real name.
		return fmt.Errorf("edge: %s digest %s, manifest says %s", e.Name, got, e.SHA256)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("edge: close %s: %w", e.Name, err)
	}
	if err := os.Rename(tmpName, full); err != nil {
		// On Windows Rename refuses an existing target. Removing first is safe
		// here specifically because the file we are replacing failed the digest
		// check earlier, or never existed.
		if rmErr := os.Remove(full); rmErr == nil || os.IsNotExist(rmErr) {
			if err2 := os.Rename(tmpName, full); err2 == nil {
				c.progress("edge: %s ok", e.Name)
				return nil
			}
		}
		return fmt.Errorf("edge: install %s: %w", e.Name, err)
	}
	c.progress("edge: %s ok", e.Name)
	return nil
}

// open issues the GET for one entry, retrying transient failures.
func (c *Client) open(ctx context.Context, e Entry) (io.ReadCloser, error) {
	url := c.BaseURL + "/artifact/" + e.Name
	var lastErr error
	attempts := c.MaxRetries
	if attempts < 1 {
		attempts = 1
	}
	for attempt := 1; attempt <= attempts; attempt++ {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return nil, err
		}
		resp, err := c.http().Do(req)
		if err != nil {
			lastErr = err
		} else if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusPartialContent {
			// A 404 or 405 will not change on retry, so fail now.
			drain(resp)
			if resp.StatusCode == http.StatusNotFound {
				return nil, fmt.Errorf("edge: %s not published: %w", e.Name, ErrNotFound)
			}
			return nil, fmt.Errorf("edge: %s: status %d", e.Name, resp.StatusCode)
		} else {
			return resp.Body, nil
		}
		if attempt < attempts {
			// Exponential backoff, because the failure being retried is
			// overwhelmingly a Space waking up or a proxy hiccup, both of which
			// clear in seconds.
			wait := time.Duration(1<<uint(attempt-1)) * time.Second
			c.logf("edge: %s attempt %d/%d failed (%v); retrying in %s", e.Name, attempt, attempts, lastErr, wait)
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(wait):
			}
		}
	}
	return nil, fmt.Errorf("edge: %s: %w", e.Name, lastErr)
}

// copyProgress copies exactly n bytes, hashing as it goes.
//
// The hash is computed on the same stream that is written to disk, so there is
// no second pass over gigabytes and no chance of the two disagreeing.
func copyProgress(dst io.Writer, h hash.Hash, src io.Reader, n int64, progress func(string, ...any)) (int64, error) {
	const window = 8 << 20
	buf := make([]byte, window)
	var done int64
	for done < n {
		want := n - done
		if want > window {
			want = window
		}
		chunk := buf[:want]
		read, err := io.ReadFull(src, chunk)
		if read > 0 {
			if _, werr := h.Write(chunk[:read]); werr != nil {
				return done, werr
			}
			if _, werr := dst.Write(chunk[:read]); werr != nil {
				return done, werr
			}
			done += int64(read)
		}
		if err != nil {
			if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
				// Short read: the caller reports it against the expected size, so
				// the message names the artefact rather than "EOF".
				return done, io.ErrUnexpectedEOF
			}
			return done, err
		}
	}
	return done, nil
}

func drain(resp *http.Response) {
	if resp == nil || resp.Body == nil {
		return
	}
	// Draining lets the connection go back to the idle pool instead of being
	// torn down; the limit keeps a lying Content-Length from costing real time.
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
	resp.Body.Close()
}

// ContentLengthOf reads a remote artefact's length without downloading it, which
// is how the notebook can print a progress table before committing to anything.
func (c *Client) ContentLengthOf(ctx context.Context, name string) (int64, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodHead, c.BaseURL+"/artifact/"+name, nil)
	if err != nil {
		return 0, err
	}
	resp, err := c.http().Do(req)
	if err != nil {
		return 0, err
	}
	defer drain(resp)
	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("edge: HEAD %s: status %d", name, resp.StatusCode)
	}
	n, err := strconv.ParseInt(resp.Header.Get("Content-Length"), 10, 64)
	if err != nil {
		return 0, fmt.Errorf("edge: HEAD %s: no usable Content-Length", name)
	}
	return n, nil
}