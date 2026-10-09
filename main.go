// Package main implements tgup, T_Dubber's Telegram uploader.
//
// Why this program exists
// -----------------------
// Measured on the target machine: uplink 3.76 MB/s, round trip 110 ms, and a
// single-stream upload that settled at 2.06 MB/s. That is 55% of the link, and
// the arithmetic explains why. Pushing 3.76 MB/s across a 110 ms round trip
// needs roughly 404 KB in flight at any instant -- the bandwidth-delay product.
// The observed rate implies an effective per-connection window near 232 KB,
// which is the textbook symptom of a window too small to keep a long pipe full.
//
// No language can make one socket's window larger; that is a kernel and network
// property, not a language one. Several sockets at once can, though. Three
// concurrent uploads carry roughly three windows, which clears 404 KB. That is
// why this is Go: overlapping work across connections is natural here.
//
// The part that is easy to get wrong
// ---------------------------------
// Goroutines alone do nothing for throughput. A gotd/td client owns one MTProto
// connection per DC, so N goroutines sharing one client still share one TCP
// window and the rate does not move. Real concurrency means N clients, each
// with its own connection. So this program opens a pool of clients, gives each
// one its own copy of the session file (same auth key, so one login covers all
// of them), and hands one part at a time to whichever client is free.
//
// What it is, and is not
// ---------------------
// It is the uploader. It does not reimplement Telegram's protocol: gotd/td is
// used as the MTProto library, the same role Telethon plays on the Python side.
// It does not touch the Gradio UI, the Kaggle orchestrator, the SQLite index, or
// the manifest format -- the manifest it writes is exactly what the Python
// restore path already reads, so archives move freely in both directions.
//
// Failure behaviour
// -----------------
// Any problem here -- no toolchain, a blocked binary, missing credentials, a
// failed login -- is reported as a clean error and the Python caller falls back
// to Telethon. Uploading never depends on this binary existing.
//
// Commands
// --------
//
//	plan    split a file and hash each part (offline)
//	upload  send the parts over several connections, from a file or a URL
//	fetch   pull an archive back and verify every part
//	bench   measure single-stream vs concurrent throughput
package main

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gotd/td/session"
	"github.com/gotd/td/telegram"
	"github.com/gotd/td/telegram/auth"
	"github.com/gotd/td/telegram/message"
	"github.com/gotd/td/tg"
)

const (
	// Telegram's MTProto user API caps a single upload at 2 GB for ordinary
	// accounts and 4 GB for Premium. 1900 MB stays under the lower ceiling with
	// room for transport overhead, so one chunk size fits every account type.
	DefaultChunkSize = int64(1900 * 1024 * 1024)

	// Telegram renders document artwork inline only for small files. Anything
	// larger becomes a streaming-video preview instead, so this uploader records
	// the cover path in the manifest for the app's own use rather than attaching
	// it and letting every client try to stream a 9 GB part.
	// The Python path via Telethon still owns real artwork attachments.
	MaxThumbnailBytes = int64(10 * 1024 * 1024)

	manifestKey = "tg_dubber_manifest"
	manifestVer = 2
)

// defaultSessionFile is where tgup looks for its session when neither --session
// nor TGUP_SESSION says otherwise. It is relative on purpose: the binary has no
// opinion about which directory it lives in, so the working directory -- the one
// place every caller already controls -- decides.
const defaultSessionFile = "tgup.session"

// sessionFile is where this run reads and writes its gotd session. It is a var
// because --session and TGUP_SESSION must be able to point it at a directory
// that survives the process (on a worker: /kaggle/working), and every login and
// every session copy is derived from it.
var sessionFile = defaultSessionFile

// maxConcurrency bounds sockets in flight. Telegram rate-limits bursts, so past
// a point extra connections stop helping and start earning FloodWaitErrors.
const maxConcurrency = 8

// ---------------------------------------------------------------------------
// Data types
// ---------------------------------------------------------------------------

// Part is one slice of the source file.
type Part struct {
	Number int    `json:"part"`
	Offset int64  `json:"offset"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}

// Plan is the on-disk layout: how a file divides and what each part hashes to.
// A part carrying a SHA256 in a saved plan has already been stored, which is
// what makes resume work.
//
// Source records where the bytes came from (an absolute path, or a redacted
// URL). It is not decoration: resume trusts the offsets in this file, so a plan
// built from a different payload would stitch together bytes from two files
// that have nothing in common. see sameSource in source.go.
type Plan struct {
	Version   int    `json:"version"`
	ChunkSize int64  `json:"chunk_size"`
	TotalSize int64  `json:"total_size"`
	Filename  string `json:"filename"`
	Source    string `json:"source,omitempty"`
	CreatedAt string `json:"created_at"`
	Parts     []Part `json:"parts"`
}

// StoredPart is a part after Telegram has confirmed it.
type StoredPart struct {
	Part      int    `json:"part"`
	Offset    int64  `json:"offset"`
	Size      int64  `json:"size"`
	SHA256    string `json:"sha256"`
	Name      string `json:"name"`
	MessageID int    `json:"message_id"`
	Link      string `json:"link"`
}

// Manifest is the descriptor posted as the album's last message. Field names
// match what the Python restore path already expects.
type Manifest struct {
	Marker        string       `json:"tg_dubber_manifest"`
	Version       int          `json:"version"`
	Filename      string       `json:"filename"`
	Size          int64        `json:"size"`
	ChunkSize     int64        `json:"chunk_size"`
	Chunked       bool         `json:"chunked"`
	ChunkCount    int          `json:"chunk_count"`
	Channel       string       `json:"channel"`
	Caption       string       `json:"caption,omitempty"`
	SourceSHA256  string       `json:"source_sha256,omitempty"`
	ThumbnailPath string       `json:"thumbnail_path,omitempty"`
	CreatedAt     string       `json:"created_at"`
	App           string       `json:"app"`
	Parts         []StoredPart `json:"parts"`
}

// Result is the machine-readable summary handed back to Python.
type Result struct {
	OK            bool         `json:"ok"`
	Error         string       `json:"error,omitempty"`
	Mode          string       `json:"mode"`
	Channel       string       `json:"channel"`
	Filename      string       `json:"filename"`
	TotalSize     int64        `json:"total_size"`
	Chunked       bool         `json:"chunked"`
	ChunkCount    int          `json:"chunk_count"`
	Concurrency   int          `json:"concurrency"`
	MessageID     int          `json:"message_id,omitempty"`
	MessageLink   string       `json:"message_link,omitempty"`
	Parts         []StoredPart `json:"parts"`
	SkippedParts  []int        `json:"resume_skipped,omitempty"`
	ElapsedSec    float64      `json:"elapsed_sec"`
	BytesPerSec   float64      `json:"bytes_per_sec"`
	SourceSHA256  string       `json:"source_sha256,omitempty"`
	VerifiedBytes int64        `json:"verified_bytes,omitempty"`
	VerifiedOK    bool         `json:"verified_ok,omitempty"`
}

// Progress goes to stderr so stdout stays free for the final result. The Python
// caller reads these lines to drive its progress bar.
type Progress struct {
	Event       string  `json:"event"`
	Part        int     `json:"part,omitempty"`
	PartCount   int     `json:"part_count,omitempty"`
	Bytes       int64   `json:"bytes,omitempty"`
	Total       int64   `json:"total,omitempty"`
	BytesPerSec float64 `json:"bytes_per_sec,omitempty"`
	ElapsedSec  float64 `json:"elapsed_sec,omitempty"`
	Message     string  `json:"message,omitempty"`
}

// credentials arrive as flags so nothing sensitive is baked into the binary.
type credentials struct {
	apiID   int
	apiHash string
	phone   string
}

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

func humanBytes(n float64) string {
	units := []string{"B", "KB", "MB", "GB", "TB", "PB"}
	i := 0
	for n >= 1024 && i < len(units)-1 {
		n /= 1024
		i++
	}
	return fmt.Sprintf("%.2f %s", n, units[i])
}

func max64(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}

func nowStamp() string { return time.Now().Format("2006-01-02T15:04:05-0700") }

func trimAt(s string) string { return strings.TrimPrefix(strings.TrimSpace(s), "@") }

func logf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
}

func emit(p Progress) {
	if data, err := json.Marshal(p); err == nil {
		fmt.Fprintln(os.Stderr, string(data))
	}
}

func writeJSON(path string, v any) error {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o644)
}

// writeResult is deliberately quiet on failure: losing the summary must never
// turn a successful upload into a reported failure.
func writeResult(path string, r Result) {
	if path == "" {
		return
	}
	if err := writeJSON(path, r); err != nil {
		logf("warning: could not write the result file: %v", err)
	}
}

func readJSON(path string, v any) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, v)
}

// partName keeps every album message self-describing, so a channel reads
// "part 2 of 5" without anyone opening the manifest.
func partName(base string, number, total int) string {
	if total <= 1 {
		return base
	}
	ext := filepath.Ext(base)
	return fmt.Sprintf("%s.part%04dof%04d%s",
		strings.TrimSuffix(base, ext), number, total, ext)
}

func sanitise(name string) string {
	if name == "" {
		return "downloaded.bin"
	}
	replacer := strings.NewReplacer(
		"/", "_", "\\", "_", ":", "_", "*", "_",
		"?", "_", "\"", "_", "<", "_", ">", "_", "|", "_")
	clean := replacer.Replace(name)
	if clean == "." || clean == ".." {
		return "downloaded.bin"
	}
	return clean
}

// hashRange streams a byte range. A multi-gigabyte part is never buffered, so
// memory stays flat no matter how large the part is.
func hashRange(path string, offset, size int64) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return "", err
	}
	h := sha256.New()
	if _, err := io.Copy(h, io.LimitReader(f, size)); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func hashWhole(path string) (string, error) {
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

func buildPlan(name, source string, size, chunkSize int64) Plan {
	if chunkSize <= 0 {
		chunkSize = DefaultChunkSize
	}
	plan := Plan{
		Version:   1,
		ChunkSize: chunkSize,
		TotalSize: size,
		Filename:  sanitise(filepath.Base(name)),
		Source:    source,
		CreatedAt: nowStamp(),
	}
	if size <= chunkSize {
		plan.Parts = append(plan.Parts, Part{Number: 1, Size: size})
		return plan
	}
	total := (size + chunkSize - 1) / chunkSize
	for i := int64(0); i < total; i++ {
		offset := i * chunkSize
		length := size - offset
		if length > chunkSize {
			length = chunkSize
		}
		plan.Parts = append(plan.Parts, Part{
			Number: int(i + 1), Offset: offset, Size: length,
		})
	}
	return plan
}

func readPlan(path string) (Plan, error) {
	var plan Plan
	if err := readJSON(path, &plan); err != nil {
		return Plan{}, err
	}
	if plan.ChunkSize <= 0 {
		plan.ChunkSize = DefaultChunkSize
	}
	if plan.Filename == "" {
		plan.Filename = filepath.Base(path)
	}
	return plan, nil
}

// ---------------------------------------------------------------------------
// Authentication
// ---------------------------------------------------------------------------

// consoleAuth satisfies gotd's UserAuthenticator by asking the terminal for the
// one thing it cannot guess: the login code. Everything else is either known up
// front or never needed.
type consoleAuth struct {
	phone string
	in    *bufio.Reader
}

func (a consoleAuth) Phone(context.Context) (string, error) { return a.phone, nil }

func (a consoleAuth) Code(_ context.Context, _ *tg.AuthSentCode) (string, error) {
	fmt.Fprint(os.Stderr, "Login code Telegram sent: ")
	line, err := a.in.ReadString('\n')
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(line), nil
}

// Password is only consulted when the account actually has 2FA on.
func (a consoleAuth) Password(context.Context) (string, error) {
	fmt.Fprint(os.Stderr, "Two-factor password: ")
	line, err := a.in.ReadString('\n')
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(line), nil
}

// AcceptTermsOfService turns a SignUpRequired into a clear message rather than a
// silent hang: this uploader signs into an existing account, it never creates
// one, and an unregistered number is a configuration mistake worth reporting.
func (a consoleAuth) AcceptTermsOfService(_ context.Context, tos tg.HelpTermsOfService) error {
	return &auth.SignUpRequired{TermsOfService: tos}
}

func (a consoleAuth) SignUp(context.Context) (auth.UserInfo, error) {
	return auth.UserInfo{}, errors.New(
		"this number is not registered on Telegram yet; sign up in the official app first")
}

// newClient builds a client bound to one specific session file. That file is the
// only thing giving it its own TCP connection, which is the entire point of
// running several of them.
func newClient(cred credentials, sessionPath string) *telegram.Client {
	return telegram.NewClient(cred.apiID, cred.apiHash, telegram.Options{
		SessionStorage: &session.FileStorage{Path: sessionPath},
		// Updates are noise for an uploader; refusing them removes a background
		// stream of traffic that would otherwise compete with the upload.
		NoUpdates: true,
	})
}

// canPromptForCode reports whether os.Stdin is a terminal -- that is, whether a
// login code Telegram sends could actually be typed back in.
//
// This is the switch that stops the OTP spam. Telegram dispatches the code the
// moment the auth flow starts, so asking for one with a pipe, a redirected file
// or no console at all (every Kaggle worker) spends a real message on a request
// that can only end in EOF. The failure was never the login itself; it was
// starting one that had no way to finish.
func canPromptForCode() bool {
	info, err := os.Stdin.Stat()
	if err != nil {
		return false
	}
	return info.Mode()&os.ModeCharDevice != 0
}

// loginDecision is ensureAuthorized's reasoning, split out so the rule "never
// request a code nobody can answer" is testable without a network. Nothing that
// can dispatch a code runs before it.
func loginDecision(authorized bool, phone string, promptable bool, sessionPath string) error {
	if authorized {
		return nil
	}
	if phone == "" {
		return errors.New(
			"not authorized yet and no --phone given; run once with --phone to log in")
	}
	if !promptable {
		return fmt.Errorf(
			"no authorized session at %s and stdin is not a terminal; refusing to "+
				"request a login code (log in once from a console, or point "+
				"--session/$TGUP_SESSION at a session that already exists)",
			sessionPath)
	}
	return nil
}

// ensureAuthorized logs in only when the stored session is not usable, so a
// machine that has run this before is never asked for a code again.
func ensureAuthorized(ctx context.Context, client *telegram.Client, phone string, in *bufio.Reader) error {
	status, err := client.Auth().Status(ctx)
	authorized := err == nil && status != nil && status.Authorized
	if decisionErr := loginDecision(authorized, phone, canPromptForCode(), sessionFile); decisionErr != nil {
		return decisionErr
	}
	flow := auth.NewFlow(consoleAuth{phone: phone, in: in}, auth.SendCodeOptions{
		AllowFlashCall: false,
	})
	if err := flow.Run(ctx, client.Auth()); err != nil {
		return fmt.Errorf("login failed: %w", err)
	}
	return nil
}

// ---------------------------------------------------------------------------
// The connection pool
// ---------------------------------------------------------------------------

// conn is one MTProto connection plus a ready-made request builder. Two of
// these running at once is what actually doubles the throughput ceiling.
type conn struct {
	label  string
	client *telegram.Client
	sender *message.Sender
	peer   *message.RequestBuilder
	// cancel ends this connection. gotd exposes no Close, so cancelling the
	// per-connection context is the only way to shut one down cleanly.
	cancel context.CancelFunc
}

// pool is a set of connections that all share one auth key, which Telegram
// permits: the same account may hold several sessions on one device. Each one
// gets its own copy of the session file because gotd writes state back to it.
type pool struct {
	conns  []*conn
	mu     sync.Mutex
	cursor int
	closed sync.Once
}

// next hands out connections round-robin so every part gets an equal share of
// the link rather than one worker hoarding it.
func (p *pool) next() *conn {
	p.mu.Lock()
	defer p.mu.Unlock()
	c := p.conns[p.cursor%len(p.conns)]
	p.cursor++
	return c
}

func (p *pool) close() {
	p.closed.Do(func() {
		for _, c := range p.conns {
			if c.cancel != nil {
				c.cancel()
			}
		}
	})
}

// sessionCopies duplicates the base session file once per connection. It runs
// before any client opens it, so no client ever sees a half-written file.
func sessionCopies(count int) ([]string, error) {
	// --session and TGUP_SESSION may name a directory that does not exist yet
	// (a fresh /kaggle/working). Creating it here turns a confusing write error
	// deep inside gotd into the obvious thing having happened.
	if dir := filepath.Dir(sessionFile); dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, fmt.Errorf("creating session directory %s: %w", dir, err)
		}
	}
	data, err := os.ReadFile(sessionFile)
	if err != nil {
		// No session yet: create empty ones. gotd treats a missing file as a
		// fresh session, and the first client will log in and write it out.
		data = nil
	}
	paths := make([]string, 0, count)
	for i := 0; i < count; i++ {
		p := fmt.Sprintf("%s.conn%d", sessionFile, i)
		if data != nil {
			if err := os.WriteFile(p, data, 0o600); err != nil {
				return nil, err
			}
		} else {
			_ = os.Remove(p)
		}
		paths = append(paths, p)
	}
	return paths, nil
}

// openPool starts `count` connections and waits until each is authorized and
// ready to send. If any one fails, the ones already up are closed, so a failed
// start never leaks a running connection.
func openPool(ctx context.Context, cred credentials, channel string, count int, in *bufio.Reader) (*pool, error) {
	if count < 1 {
		count = 1
	}
	if count > maxConcurrency {
		count = maxConcurrency
	}

	paths, err := sessionCopies(count)
	if err != nil {
		return nil, fmt.Errorf("preparing session copies: %w", err)
	}
	defer func() {
		for _, p := range paths {
			_ = os.Remove(p)
		}
	}()

	// The first connection does the login. Its session file becomes the one
	// future runs reuse, so a second pass never asks for a code again.
	logf("session   %s", sessionFile)
	base := newClient(cred, sessionFile)
	baseCtx, cancelBase := context.WithCancel(ctx)
	baseErr := base.Run(baseCtx, func(ctx context.Context) error {
		return ensureAuthorized(ctx, base, cred.phone, in)
	})
	// Whether it just logged in or was already authorized, the session file now
	// holds a usable auth key either way. Cancelling is how the connection ends.
	cancelBase()
	if baseErr != nil {
		return nil, baseErr
	}

	// Refresh the copies now that a real session exists.
	if data, err := os.ReadFile(sessionFile); err == nil {
		for _, path := range paths {
			if err := os.WriteFile(path, data, 0o600); err != nil {
				return nil, fmt.Errorf("copying session: %w", err)
			}
		}
	}

	p := &pool{}
	ready := make(chan error, count)
	var wg sync.WaitGroup

	for i, path := range paths {
		client := newClient(cred, path)
		connCtx, cancelConn := context.WithCancel(ctx)
		entry := &conn{
			label:  fmt.Sprintf("conn%d", i),
			client: client,
			cancel: cancelConn,
		}
		// Each goroutine reports readiness exactly once. Reporting again on the
		// way down would block forever on a full channel, because a healthy
		// connection only errors once its context is cancelled.
		var reported sync.Once
		report := func(err error) { reported.Do(func() { ready <- err }) }

		wg.Add(1)
		go func() {
			defer wg.Done()
			err := client.Run(connCtx, func(ctx context.Context) error {
				if err := ensureAuthorized(ctx, client, "", in); err != nil {
					return err
				}
				api := tg.NewClient(client)
				entry.sender = message.NewSender(api).
					WithUploader(newTunedUploader(api))
				entry.peer = entry.sender.Resolve(trimAt(channel))
				report(nil)
				// Stay connected for as long as the caller needs it.
				<-ctx.Done()
				return ctx.Err()
			})
			report(err)
		}()
		p.conns = append(p.conns, entry)
	}

	for range paths {
		if err := <-ready; err != nil {
			p.close()
			wg.Wait()
			return nil, err
		}
	}

	// Release the per-connection goroutines when the caller is done.
	go func() {
		wg.Wait()
		p.close()
	}()
	return p, nil
}

// signalContext makes Ctrl+C stop cleanly: an interrupted upload still gets its
// plan written and can be resumed instead of starting over.
func signalContext() (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(context.Background())
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	go func() {
		<-sig
		logf("interrupted: letting the parts in flight finish, then saving the plan")
		cancel()
	}()
	return ctx, cancel
}
