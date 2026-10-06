// edge-fetch is the Kaggle-side client.
//
// WHAT IT IS FOR
// --------------
// The notebook's setup cell spends 903 s of a 1220 s run downloading things that
// did not change (run 14: 378 s of pip, 525 s of model weights). This binary is
// the cache layer that turns that into ~8 s on a warm run.
//
// It works in three steps, in this order, because that order is what makes the
// warm path cheap:
//
//	1. GET /manifest.json            one small request
//	2. hash what is already on disk  local reads only
//	3. GET only the entries that disagree
//
// Step 2 is local, so a fully warm run transfers zero bytes and pays only step
// 1. The alternative -- always re-download and let something else cache -- is
// what makes the run slow.
//
// WHY A BINARY AND NOT A PYTHON CELL
// ---------------------------------
// It has to work before the interpreter that needs the artefacts is usable. The
// pylibs tree is the artefact in question, so a Python helper would need a
// Python environment, which is exactly what is missing during setup. A static Go
// binary has no such ordering problem. It also runs in about 40 ms, which is
// noise next to the 8 s it saves.
//
// FAILURE IS NEVER FATAL
// ----------------------
// If the Space is down, asleep, behind a proxy that rejects us, or publishing
// nothing, this exits 0 and says so. The caller then falls back to origin. A
// cache miss must never be worse than no cache at all, and that property is
// tested rather than asserted.

package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/engrtarun/tgup/edge"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "edge-fetch:", err)
		os.Exit(1)
	}
}

func run() error {
	var (
		baseURL = flag.String("url", os.Getenv("EDGE_URL"), "Space base URL, e.g. https://user-space.hf.space")
		dest    = flag.String("dest", envOr("EDGE_DEST", "./artefacts"), "destination directory")
		role    = flag.String("role", os.Getenv("EDGE_ROLE"), "restrict to a role: pylibs, weights, pack, or empty for all")
		timeout = flag.Duration("timeout", 0, "overall deadline; 0 means none")
		// retries is deliberately small. This runs inside a notebook cell whose
		// own timeout is the real bound; spinning here delays the fallback.
		retries = flag.Int("retries", 3, "attempts per artefact")
		// mustFetch means "fail if anything is missing", which is what a
		// verification pass wants. The default is false so an unreachable Space
		// degrades to a fallback instead of a failed run.
		mustFetch = flag.Bool("must-fetch", envBool("EDGE_MUST_FETCH", false),
			"exit non-zero when the Space cannot supply everything, instead of falling back")
		quiet = flag.Bool("quiet", false, "only print the final summary line")
		planOnly = flag.Bool("plan", false, "print the plan and exit without transferring anything")
		// Unpacking is where a downloaded archive becomes a directory the
		// next cell can import from; without it a fetched pylibs.tar.gz is
		// just bytes sitting in a scratch directory.
		unpackDir = flag.String("unpack-dir", "",
			"extract verified archives into this directory (empty: leave archives as they are)")
		// Archives are dropped after a successful unpack by default: pylibs
		// plus weights is ~7 GB compressed on top of ~9 GB unpacked, and
		// /kaggle/working has a 20 GB output quota.
		keepArchives = flag.Bool("keep-archives", false,
			"keep each archive after unpacking it instead of deleting it")
	)
	flag.Parse()

	if *baseURL == "" {
		// Nothing to do, and it is the common case in local development and in
		// any test run. Silent success keeps every caller from having to guard.
		fmt.Println("edge-fetch: EDGE_URL is not set, skipping cache")
		return nil
	}

	ctx := context.Background()
	if *timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, *timeout)
		defer cancel()
	}

	pf := func(format string, args ...any) {
		if !*quiet {
			fmt.Printf(format+"\n", args...)
		}
	}
	lf := func(format string, args ...any) {
		if !*quiet {
			fmt.Fprintf(os.Stderr, format+"\n", args...)
		}
	}

	c := edge.NewClient(*baseURL)
	c.Progress = pf
	c.Log = lf
	c.MaxRetries = *retries

	started := time.Now()

	// A Space that is cold answers /healthz slowly. Ping before the manifest so
	// "the Space is asleep" and "the Space has nothing published" are separate
	// log lines, because the fixes are different: wait, versus fall back.
	if err := c.Ping(ctx); err != nil {
		if *mustFetch {
			return fmt.Errorf("edge Space unreachable and -must-fetch was set: %w", err)
		}
		fmt.Fprintf(os.Stderr, "edge-fetch: Space unreachable (%v), falling back to origin\n", err)
		return nil
	}

	m, err := c.FetchManifest(ctx)
	if err != nil {
		if *mustFetch {
			return err
		}
		fmt.Fprintf(os.Stderr, "edge-fetch: manifest unavailable (%v), falling back to origin\n", err)
		return nil
	}

	if *role != "" {
		kept := m.ByRole(*role)
		if len(kept) == 0 {
			if *mustFetch {
				return fmt.Errorf("edge Space publishes no %q artefacts", *role)
			}
			fmt.Fprintf(os.Stderr, "edge-fetch: no %q artefacts published, falling back to origin\n", *role)
			return nil
		}
		m.Entries = kept
	}
	if len(m.Entries) == 0 {
		if *mustFetch {
			return errors.New("edge Space publishes no artefacts and -must-fetch was set")
		}
		fmt.Fprintln(os.Stderr, "edge-fetch: Space has published nothing, falling back to origin")
		return nil
	}

	plan, err := c.PlanAgainst(m, *dest)
	if err != nil {
		if *mustFetch {
			return err
		}
		fmt.Fprintf(os.Stderr, "edge-fetch: plan failed (%v), falling back to origin\n", err)
		return nil
	}

	// The plan line is what the notebook's own log needs: it is the difference
	// between "5.2 GB to download" and "already current", stated before any
	// bandwidth is committed.
	pf("edge: %d artefact(s), %d current, %d to fetch (%.1f MiB)",
		len(plan.Entries), len(plan.Current), len(plan.Missing),
		float64(plan.TotalNew)/(1<<20))

	if *planOnly {
		fmt.Printf("plan_only=1 missing=%d current=%d total_new_bytes=%d\n",
			len(plan.Missing), len(plan.Current), plan.TotalNew)
		return nil
	}

	// Fetch first, unpack second, and only ever unpack what the digest check
	// has already passed: the archive is about to become a directory that a
	// later cell imports code from, so verification and extraction must not be
	// two different trust decisions.
	fetchErr := c.Fetch(ctx, *dest, plan)
	if fetchErr != nil {
		if *mustFetch {
			return fetchErr
		}
		// Partial success is still success for the caller's purposes: whatever
		// arrived is digest-verified and safe to use, and the rest falls back.
		fmt.Fprintf(os.Stderr, "edge-fetch: some artefacts failed (%v), falling back for those\n", fetchErr)
	}

	unpacked, unpackFailed := unpackPlan(*dest, *unpackDir, plan,
		fetchErr == nil, !*keepArchives, pf, lf)
	if unpackFailed > 0 && *mustFetch {
		return fmt.Errorf("edge-fetch: %d archive(s) could not be unpacked", unpackFailed)
	}

	fmt.Printf("edge_fetch_ok=1 current=%d fetched=%d unpacked=%d elapsed_s=%d\n",
		len(plan.Current), len(plan.Missing), unpacked, int(time.Since(started).Seconds()))
	return nil
}

// unpackPlan extracts every archive in the plan into unpackDir.
//
// trustFetched says Fetch completed without error, which means every entry in
// plan.Missing was verified at the moment it was renamed into place. That is
// the whole reason the second hash can be skipped: re-hashing 7 GB of freshly
// downloaded archives to learn what the download already proved costs about
// two seconds of CPU and a pass of disk reads on a cold Kaggle run.
//
// When Fetch reported a failure the flag is false and every archive is
// re-verified from disk, because a failed fetch can leave the PREVIOUS
// run's mismatched file sitting at the target path. Trusting a name rather
// than a digest would be exactly the bug the manifest exists to prevent.
//
// Already-current entries are always re-verified: they were verified by a
// different process, minutes or days ago.
func unpackPlan(dest, unpackDir string, plan *edge.Plan, trustFetched, dropAfter bool,
	pf, lf func(format string, args ...any)) (unpacked, failed int) {

	if unpackDir == "" {
		return 0, 0
	}

	freshlyFetched := make(map[string]bool, len(plan.Missing))
	if trustFetched {
		for _, e := range plan.Missing {
			freshlyFetched[e.Name] = true
		}
	}

	seen := make(map[string]bool, len(plan.Entries))
	for _, e := range plan.Entries {
		if seen[e.Name] || !edge.IsArchive(e.Name) {
			continue
		}
		seen[e.Name] = true

		full, err := edge.Resolve(dest, e.Name)
		if err != nil {
			lf("edge: cannot place %s for unpacking: %v", e.Name, err)
			failed++
			continue
		}
		info, err := os.Stat(full)
		if err != nil {
			// Not on disk: the download for this entry failed and Fetch has
			// already named it. Nothing to unpack.
			continue
		}
		// Size is checked in every case: it is free, and it catches a truncated
		// or partial file before anything reads it.
		if info.Size() != e.SizeBytes {
			lf("edge: not unpacking %s: %d bytes on disk, manifest says %d",
				e.Name, info.Size(), e.SizeBytes)
			failed++
			continue
		}
		if !freshlyFetched[e.Name] {
			sum, err := edge.FileSHA256(full)
			if err != nil || !strings.EqualFold(sum, e.SHA256) {
				lf("edge: not unpacking %s: digest mismatch (on disk %s, manifest %s)",
					e.Name, sum, e.SHA256)
				failed++
				continue
			}
		}

		res, err := edge.Unpack(full, unpackDir)
		if err != nil {
			lf("edge: unpack %s failed: %v", e.Name, err)
			failed++
			continue
		}
		unpacked++
		pf("edge: unpacked %s -> %s (%d files, %.1f MiB%s)",
			e.Name, res.Target, res.Files, float64(res.Bytes)/(1<<20),
			skippedSuffix(res.Skipped))
		for _, w := range res.Warns {
			lf("edge:   refused: %s", w)
		}
		if dropAfter {
			// Removed only after a successful unpack: a failed extraction
			// keeps the archive for a retry instead of costing another
			// multi-gigabyte download.
			if err := os.Remove(full); err != nil {
				lf("edge: could not remove %s after unpacking: %v", e.Name, err)
			}
		}
	}
	return unpacked, failed
}

func skippedSuffix(n int) string {
	if n == 0 {
		return ""
	}
	return fmt.Sprintf(", %d member(s) refused", n)
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
	switch strings.ToLower(v) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return fallback
	}
}