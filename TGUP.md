# tgup — the multi-connection Telegram uploader

Python + Telethon still does everything it did before. This is an **addition**,
not a replacement: an optional uploader that sends a file's parts over several
Telegram connections at once, so a link that a single stream leaves half idle
gets used.

---

## Why it exists

Measured on this machine:

| | |
|---|---|
| Uplink (speedtest) | **3.76 MB/s** (30.05 Mbit/s) |
| Round trip | **110 ms** |
| Observed single-stream upload | **2.06 MB/s** |
| Link utilisation | **55%** |

The 55% is arithmetic, not bad code. To carry 3.76 MB/s across a 110 ms round
trip you need about

```
3.76 MB/s × 0.110 s ≈ 404 KB
```

in flight at any instant. That is the **bandwidth–delay product**. The observed
2.06 MB/s implies an effective per-connection window near **232 KB** — the
textbook symptom of a window too small to keep a long pipe full.

**No language makes one socket's window bigger.** That is a kernel and network
property. Several sockets at once can: three concurrent uploads put roughly three
windows in flight, which clears 404 KB.

That is the entire reason this program is written in Go. Goroutines make
overlapping work across connections natural, and the rest is just plumbing.

### The part that is easy to get wrong

Goroutines alone buy nothing here.

A `gotd/td` client owns **one** MTProto connection per DC. N goroutines sharing
one client still share one TCP window, and the rate does not move. So `tgup`
opens a **pool of separate clients** — each with its own copy of the session
file, which is cheap because they all share one auth key and therefore one login.
That pool is the whole mechanism.

## What Go is used for

1. **`plan`** — split a file and SHA-256 each part. Offline, no credentials.
   Also the manifest's authoritative digests, and a **cross-check**: the Python
   uploader hashes again as the bytes stream, and a mismatch means the file
   changed mid-upload. With whole digests that is now a real equality test rather
   than a prefix comparison.
2. **`upload`** — the speed path, for **multi-part** files on **public**
   channels. Every failure raises so the caller falls back to Telethon.
3. **`fetch`** — restore an archive, several parts at once, verifying each part
   before it is joined into the file.
4. **`bench`** — measure whether any of this actually helped.

## What Go is *not* used for

Not the orchestrator, the Gradio UI, the Kaggle pipeline, the SQLite index, the
manifest format, artwork/thumbnail attachments, or private-channel `tg://` links.
Telethon keeps all of that, and `telegram_uploader.py` has **no import** of this
project — a test in `test_tgup.py` enforces that.

The archive format is shared: `tgup` writes the manifest `telegram_uploader.py`
already reads, so an archive made by the fast path restores in the app, and one
made in the app can be restored by `tgup fetch`. `test_tgup.py` pins that.

---

## Honest status

**The concurrency gain is not yet measured.** Everything above is a mechanism and
a prediction. The prediction is testable, and `bench` is how:

```
python go_planner.py bench --channel @tgwebcloud1 --api-id N --api-hash H
```

It sends the same payload over 1, 2, 3, then 4 connections and prints the rate
for each. Until that has been run on this machine with this account, treat the
speed claim as unproven.

What *is* verified:

- builds, and starts (`tgup\build.ps1` refuses to report success otherwise)
- `go vet` clean, `gofmt` clean
- part layout and digests are byte-identical to the Python planner (tested)
- manifest shape accepted by the Python restore path (tested)
- Telethon path unaffected when `tgup` is absent (tested)
- 49 tests pass across `test_tgup.py`, `test_tg_cloud.py`, `test_flow_order.py`,
  `test_db.py`, `test_channel_caption.py`

## First run needs a login code

`tgup` keeps its own session file, `tgup/tgup.session`, because `gotd`'s storage
format is not Telethon's and cannot read `telegram_uploader_session.session`.
The first upload prints a prompt for the code Telegram sends; after that the
session is reused. Multiple connections share that one auth key, so it is one
login, not one per connection.

`python go_planner.py check` reports `needs_login` so you know in advance.

## Commands

```
tgup plan   --file big.mkv --plan-out plan.json
tgup upload --file big.mkv --channel @name --api-id N --api-hash H \
            --concurrency 3 --plan-out plan.json --result-out result.json
tgup upload --file big.mkv --channel @name --api-id N --api-hash H \
            --plan-in plan.json          # resume: resends only what is missing
tgup fetch  --link https://t.me/name/123 --dest .\out --api-id N --api-hash H \
            --concurrency 4
tgup bench  --channel @name --api-id N --api-hash H --concurrency 1,2,3,4
```

`upload` rewrites `--plan-out` after every stored part, so an interrupted or
crashed run resumes from where it stopped rather than starting over.

Exit codes: `0` success, `1` the operation ran and failed (credentials, network,
checksum), `2` bad usage or a missing prerequisite — nothing was sent.

## Credentials

Never stored in the binary or a config file. `--api-id` and `--api-hash` are
passed on the command line, which means a local process listing can see them
while the transfer runs. That is a deliberate tradeoff: `config.json` keeps the
hash DPAPI-encrypted, and the alternative — an env var or a credentials file —
would either leak to child processes or put it on disk in plaintext.

## Layout

```
tgup/
  main.go        types, hashing, auth, and the client pool (the core idea)
  upload.go      the concurrency engine and manifest posting
  fetch.go       restore + bench
  commands.go    plan + upload entry points
  main_entry.go  usage and dispatch
  build.ps1      build, vet, then prove the binary starts
tgup_bridge.py   finds the binary, runs it, parses its JSON progress/result
go_planner.py    adapter telegram_uploader.py already calls; same API as before
```

## Tuning

`--concurrency` defaults to 3 and is capped at 8. Use the **lowest** value that
already reaches your best rate from `bench`. Past a point extra sockets stop
helping and start earning `FLOOD_WAIT`.

`--chunk-size` defaults to 1900 MB, under Telegram's 2 GB per-file limit for
ordinary accounts. Do not raise it without a Premium account (4 GB).

`--verify=false` on `fetch` skips checksums. It is faster and it also means you
no longer know the restored file is correct. `tgup` refuses to write the final
file unless the reassembled size matches the manifest either way.