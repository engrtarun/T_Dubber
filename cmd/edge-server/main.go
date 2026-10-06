// edge-server is the Hugging Face Space binary.
//
// WHAT IT SERVES
// --------------
// An artefact tree, content-addressed by sha256, over plain HTTP with Range and
// ETag support. Nothing else. See the package doc in edge/edge.go for why the
// artefact host is the part worth putting on a Space and why the GPU work is
// not.
//
// WHY A SEPARATE BINARY AND NOT A SUBCOMMAND OF tgup
// ---------------------------------------------------
// tgup holds Telegram credentials on its command line and links gotd/td. The
// Space has no Telegram credentials, does no Telegram work, and should not link
// an MTProto stack it never calls: every dependency is a supply-chain surface
// and a binary that starts faster is one that fails to start less often on a
// cold Space. edge is dependency-free, which also means it builds to a static
// binary in a few seconds.

package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/engrtarun/tgup/edge"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "edge-server:", err)
		os.Exit(1)
	}
}

func run() error {
	var (
		root      = flag.String("root", envOr("EDGE_ROOT", "./artefacts"), "artefact root directory to serve")
		addr      = flag.String("addr", envOr("EDGE_ADDR", ":"+defaultPort()), "listen address")
		logLevel  = flag.String("log-level", envOr("EDGE_LOG_LEVEL", "info"), "debug, info, warn, error")
		logFormat = flag.String("log-format", envOr("EDGE_LOG_FORMAT", "text"), "text or json")
		// buildManifest refreshes manifest.json on disk before serving, so a
		// Space restart re-advertises whatever the persistent volume holds
		// without needing a separate publish step.
		buildManifest = flag.Bool("build-manifest", envBool("EDGE_BUILD_MANIFEST", true),
			"write manifest.json for the artefact root at startup")
		printManifest = flag.Bool("print-manifest", false,
			"print the manifest as JSON and exit, without serving")
		// printAddr writes the bound address to stdout once listening. HF Spaces
		// discovers the port from the process, but this makes a local run and a
		// container log equally readable, and it is how a ":0" test run reports.
		printAddr = flag.Bool("print-addr", true, "print the bound address once listening")
		// selfCheck performs the readiness probe and exits 0 or 1. It exists so the
		// container health check needs no curl in the image: a public endpoint has
		// no business shipping a shell, and a probe that depends on one is a probe
		// that fails for reasons unrelated to the service.
		selfCheck = flag.Bool("self-check", envBool("EDGE_SELF_CHECK", false),
			"probe this server's /healthz and exit 0 (ready) or 1 (not ready)")
	)
	flag.Parse()

	if *selfCheck {
		os.Exit(selfCheckExit(*addr))
	}

	logger, err := newLogger(*logLevel, *logFormat)
	if err != nil {
		return err
	}

	absRoot, err := absDir(*root)
	if err != nil {
		return err
	}

	if *buildManifest {
		// Hashing a multi-gigabyte tree takes time proportional to its size, so
		// it is counted rather than left to look like a hang at startup.
		start := time.Now()
		logger.Info("building manifest", "root", absRoot)
		if err := edge.PublishManifest(absRoot); err != nil {
			return err
		}
		logger.Info("manifest ready", "took", time.Since(start).Round(time.Millisecond))
	}

	if *printManifest {
		m, err := edge.BuildManifest(absRoot)
		if err != nil {
			return err
		}
		return edge.WriteManifestJSONTo(os.Stdout, m)
	}

	srv, err := edge.NewServer(edge.Config{
		ArtefactRoot: absRoot,
		Addr:         *addr,
		Logger:       logger,
		// A Space behind a proxy sends X-Forwarded-Proto; without trusting it the
		// service still works, but any future redirect would loop. Recorded in
		// the config rather than acted on, because this service never redirects.
		ShutdownGrace: 60 * time.Second,
	})
	if err != nil {
		return err
	}

	// SIGINT for a local run, SIGTERM for a Space deploy or sleep. Without this
	// the process dies mid-transfer and every client in flight has to resume;
	// with it, ListenAndServe drains first.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if *printAddr {
		// Poll the bound address so it is printed even though the listener is
		// created inside ListenAndServe. Cheap, and it removes the need for the
		// caller to know whether the address was a fixed port or ":0".
		go func() {
			for i := 0; i < 200; i++ {
				if a := srv.Addr(); a != "" {
					fmt.Printf("listening on %s\n", a)
					os.Stdout.Sync()
					return
				}
				time.Sleep(10 * time.Millisecond)
			}
		}()
	}

	if err := srv.ListenAndServe(ctx); err != nil {
		return err
	}
	logger.Info("stopped cleanly")
	return nil
}

// selfCheckExit probes the local health endpoint and returns a process exit code.
//
// The probe address is derived from addr rather than taken as a flag, because the
// health check is issued by the container runtime against the port the container
// already published. A check that asked for its own address separately would be
// two sources of truth for one number.
func selfCheckExit(addr string) int {
	host := addr
	if strings.HasPrefix(addr, ":") {
		// A wildcard listen is probed over loopback: the check is asking "is this
		// process answering", not "is this reachable from outside".
		host = "127.0.0.1" + addr
	}
	url := "http://" + host + "/healthz"

	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		fmt.Fprintf(os.Stderr, "self-check: %v\n", err)
		return 1
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, io.LimitReader(resp.Body, 4<<10))
	if resp.StatusCode != http.StatusOK {
		fmt.Fprintf(os.Stderr, "self-check: status %d\n", resp.StatusCode)
		return 1
	}
	return 0
}

func newLogger(level, format string) (*slog.Logger, error) {
	var lv slog.Level
	switch level {
	case "debug":
		lv = slog.LevelDebug
	case "info":
		lv = slog.LevelInfo
	case "warn":
		lv = slog.LevelWarn
	case "error":
		lv = slog.LevelError
	default:
		return nil, fmt.Errorf("unknown log level %q", level)
	}
	opts := &slog.HandlerOptions{Level: lv}
	switch format {
	case "text":
		return slog.New(slog.NewTextHandler(os.Stderr, opts)), nil
	case "json":
		// JSON is what a Space's log collector wants; text is what a human
		// reading `docker logs` wants. Both are supported because the same binary
		// serves both cases.
		return slog.New(slog.NewJSONHandler(os.Stderr, opts)), nil
	default:
		return nil, fmt.Errorf("unknown log format %q", format)
	}
}

// absDir resolves root and creates it if missing.
//
// Creating it is deliberate: a Space's persistent volume may not have the
// directory yet on a cold start, and refusing to boot would mean the Space can
// never serve the empty manifest a client needs to learn there is nothing here.
func absDir(root string) (string, error) {
	abs, err := absPath(root)
	if err != nil {
		return "", err
	}
	info, err := os.Stat(abs)
	switch {
	case err == nil && !info.IsDir():
		return "", fmt.Errorf("edge-server: %s exists and is not a directory", abs)
	case errors.Is(err, os.ErrNotExist):
		if err := os.MkdirAll(abs, 0o755); err != nil {
			return "", fmt.Errorf("edge-server: create %s: %w", abs, err)
		}
	case err != nil:
		return "", fmt.Errorf("edge-server: stat %s: %w", abs, err)
	}
	return abs, nil
}

func absPath(p string) (string, error) {
	return filepath.Abs(p)
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envBool(key string, fallback bool) bool {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	b, err := strconv.ParseBool(v)
	if err != nil {
		return fallback
	}
	return b
}

// defaultPort returns the port to listen on.
//
// HF Spaces routes traffic to port 7860 by default, and a Space listening
// elsewhere is reachable only through a proxy that may not be configured. So
// the default follows the platform rather than the usual 8080, and EDGE_ADDR
// overrides it for everything else.
func defaultPort() string {
	if v := os.Getenv("PORT"); v != "" {
		// Some hosts inject PORT; it wins over our own default.
		return v
	}
	return "7860"
}