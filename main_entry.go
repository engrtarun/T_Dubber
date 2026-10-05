package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
)

func jsonUnmarshal(data []byte, v any) error {
	return json.Unmarshal(data, v)
}

func usage(w io.Writer) {
	fmt.Fprint(w, `tgup - T_Dubber's Telegram uploader

Part planning, chunked uploading, archive restore, and a concurrency benchmark.
Uses gotd/td as the MTProto library, so no protocol is reimplemented here.
Python's Telethon path remains available and is used automatically whenever
this binary cannot run.

  plan    split a file into parts and hash each one (offline)
  upload  send the parts over several connections (the workhorse)
  fetch   pull an archive back down and verify every part
  bench   measure single-stream vs concurrent throughput

Every part in flight sits on its own MTProto connection, and therefore on its
own TCP receive window. That is what lifts a link that a single stream leaves
half idle. The "bench" command measures whether it worked, rather than assuming.

Credentials are never stored in the binary or in a config file: pass --api-id
and --api-hash on the command line. The login code is asked for once and the
resulting session is kept in `+"`tgup.session`"+` (override with --session or
$TGUP_SESSION, e.g. /kaggle/working/tgup.session on a worker).

A login code is only ever requested when stdin is a terminal. Telegram sends
that code the moment the flow starts, and on a machine with no console the
answer can only be EOF -- so a headless run with no session now fails up front
instead of spending an OTP to get there.

Examples
  tgup plan   --file big.mkv --plan-out plan.json
  tgup upload --file big.mkv --channel @name --api-id N --api-hash H \
              --concurrency 3 --plan-out plan.json --result-out result.json
  tgup upload --file big.mkv --channel @name --api-id N --api-hash H \
              --plan-in plan.json            # resume, resends only what is missing
  tgup fetch  --link https://t.me/name/123 --dest .\out \
              --api-id N --api-hash H --concurrency 4
  tgup bench  --channel @name --api-id N --api-hash H --concurrency 1,2,3,4

Exit codes
  0  success
  1  the operation ran but failed (credentials, network, checksum)
  2  bad usage or a missing prerequisite, nothing was sent
`)
}

func main() {
	if len(os.Args) < 2 {
		// No command is a usage error, so the help goes to stderr where it
		// cannot be mistaken for the program's real output.
		usage(os.Stderr)
		os.Exit(2)
	}
	switch os.Args[1] {
	case "plan":
		os.Exit(cmdPlan(os.Args[2:]))
	case "upload":
		os.Exit(cmdUpload(os.Args[2:]))
	case "fetch":
		os.Exit(cmdFetch(os.Args[2:]))
	case "bench":
		os.Exit(cmdBench(os.Args[2:]))
	case "-h", "--help", "help":
		// Help was asked for, so it is the result rather than an error.
		usage(os.Stdout)
		os.Exit(0)
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n\n", os.Args[1])
		usage(os.Stderr)
		os.Exit(2)
	}
}
