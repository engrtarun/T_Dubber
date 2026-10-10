---
title: T Dubber Edge
emoji: 📦
colorFrom: gray
colorTo: blue
sdk: docker
pinned: false
app_port: 7860
---

# T_Dubber Edge — artefact host

## Read this before deploying

**This Space cannot make a repeated Kaggle run warm.** An earlier draft of this
document claimed it could. It cannot, and the reason matters enough to lead
with:

> A NEW Kaggle session starts with an EMPTY `/kaggle/working`.

So anything a notebook writes there is gone before the next run. The digest
comparison in `edge-fetch` is correct, but on Kaggle it always answers "missing",
because the destination is empty. The 33 ms warm number that was measured
locally is unreachable on Kaggle.

The durable, free storage Kaggle offers is an **attached Dataset** under
`/kaggle/input`, and that already exists in the worker notebook
(`find_pylibs([INPUT, WORK])` searches it first). If you want a genuinely warm
run, attach the built tree as a Dataset. That costs zero seconds to "download"
because it is local disk.

What this Space is actually for is the **cold** run: replacing ~110 pip wheel
installations with one tar extract. It cannot save the download — the bytes are
the same either way.

## Honest cost/benefit, before you spend an hour on this

| what | saving | free? |
|---|---|---|
| pin torch in the speech pip pass | ~554 MB of duplicate download every cold run | yes, and it needs no Space |
| attach built tree as a Kaggle Dataset | the whole 458 s deps phase, on every run after the first | yes |
| **this Space** | the pip install phase on a cold run only | yes, but see above |

The first two rows are worth more than this Space and cost nothing to deploy.
The torch pin is a one-line change and is the single largest verified saving
found in the pipeline. Do that first.

If you do deploy this: free CPU Spaces **sleep** after a period of inactivity and
wake on the next request, and free-tier persistent storage is not guaranteed
across restarts. That makes a Space a worse place to keep the artefacts than an
HF Dataset or HF Hub repo, which are durable by construction. There is a real
argument for a Space (a stable URL, one origin to fetch from), but it is not the
argument "it caches between runs".

## What this Space is

An always-on HTTP file server with content-addressed artefacts. Nothing else.

## Why a Space, and not more Kaggle

Measured on Kaggle run 14 (1220 s total):

| stage | time | share | device |
|---|---|---|---|
| `pip install` of pylibs | 378 s | 31% | network |
| Homura-2B snapshot download | 525 s | 43% | network |
| TTS (OmniVoice) | 221 s | 18% | GPU |
| whisper transcribe | 40 s | 3% | GPU |
| stitch + normalize + loudness | ~10 s | 0.8% | CPU |

**903 s of the 1220 s is downloading bytes that did not change.** That is what
this Space was built to attack, and it can only reach the pip-install part of
it — see the warning at the top.

What deliberately does **not** move here: the 261 s of GPU work. A Space has no
CUDA device. Moving TTS or whisper here would mean not dubbing at all.

## Endpoint

| method | path | purpose |
|---|---|---|
| `GET`/`HEAD` | `/healthz` | liveness; touches no file |
| `GET` | `/manifest.json` | artefact list with sha256 and sizes |
| `GET`/`HEAD` | `/artifact/<name>` | one artefact, with `Range` and `ETag` |
| `GET`/`HEAD` | `/gguf/<file>` | one model blob, plus `X-Content-Sha256` |

`Range` exists because Kaggle sessions get reaped mid-download; a client resumes
from the byte it reached. `ETag` is the content digest, so a client that
already has a file can revalidate in one round trip.

The other endpoints are described under "Endpoints" below; this table is the
short version.

### `/gguf/<file>` — models, by role instead of by path

`/artifact/` is generic: it serves anything under the root and has no idea what
a model is. `/gguf/` is the same bytes addressed as a model:

* **one bare filename, nothing else.** `/gguf/ggml-base.bin`, not
  `/gguf/weights/ggml-base.bin`. Paths belong to `/artifact/`; two spellings for
  one meaning is how a client ends up guessing wrong and getting a 404 for a
  model that is sitting right there.
* **`X-Content-Sha256`** carries the blob's digest, so a client knows *what it is
  verifying against* before it commits the transfer rather than discovering it
  afterwards. It is also served as the `ETag`, for revalidation.
* **resolved from `gguf/` first**, then from any manifest entry whose role is
  `gguf`. That second lookup lets a Space that keeps models under `weights/`
  answer this route without anyone moving 2 GB of files for the sake of a URL.
* **never unpacked.** A `.gguf` is one file, not an archive, so the unpack pass
  skips it (`IsArchive` is false for `.gguf`/`.bin`).

Manifest role `gguf` classifies it, so a client can ask for `"role": "gguf"`
without knowing any file name.

The roster of what to serve — repo, file, quant, size, digest — lives in
`edge/model_gguf.go` and its Python mirror `huggingface/models_gguf.json`. A Go
test compares the two field-for-field, so a digest cannot drift between the two
sides. **A digest is never invented**: an entry with no confirmed digest carries
`SHA256Verified: false` and is served with a weak validator, which is strictly
better than a made-up one that fails verification forever.

## What it is not

- Not an orchestrator. The HTTP process has no queue and no state machine — it
  hands over bytes. The Go package does contain a ledger and a worker queue
  (`edge/ledger.go`, `edge/queue.go`) for the *controller* that runs on the
  Kaggle worker, not for this process.
- Not writable over HTTP. There is no POST, PUT or DELETE — see the security note
  below.
- Not a public mirror. Add authentication before making anything private here
  public; the service has no access control of its own.

## Security note

This service holds archives that a worker unpacks and then **executes code out
of**. Anything able to write into the artefact root can ship code to every
Kaggle run.

Two consequences are built in:

1. **No write path.** `GET` and `HEAD` only; anything else is 405.
2. **Content addressing.** Every artefact is published with a sha256 in
   `manifest.json`, and `edge-fetch` refuses to install a file whose digest does
   not match. A substituted archive cannot be delivered silently.

Path containment is enforced twice — on the cleaned relative path, then again
against the resolved absolute path — because the second check is what stops a
sibling directory that shares a name prefix (`/artefacts-evil` vs `/artefacts`)
from being reached. Both cases are covered by tests.

If you put anything private here, front it with a reverse proxy that
authenticates. The service will not do it for you.

## Endpoints

| method | path | purpose |
|---|---|---|
| `GET`/`HEAD` | `/healthz` | liveness; touches no file |
| `GET` | `/manifest.json` | artefact list with sha256 and sizes |
| `GET`/`HEAD` | `/artifact/<name>` | one artefact, with `Range` and `ETag` |
| `GET`/`HEAD` | `/gguf/<file>` | one model blob, plus `X-Content-Sha256` |

`Range` exists because Kaggle sessions get reaped mid-download; a client resumes
from the byte it reached. `ETag` is the content digest, so a warm worker gets
`304` in one round trip that transfers nothing.

## Configuration

| variable | default | meaning |
|---|---|---|
| `EDGE_ROOT` | `/data` | artefact root; also the `VOLUME` mount point |
| `EDGE_ADDR` | `:7860` | listen address (`PORT` wins if the host sets it) |
| `EDGE_LOG_FORMAT` | `json` | `json` or `text` |
| `EDGE_BUILD_MANIFEST` | `true` | hash the tree at startup |
| `EDGE_MUST_FETCH` | `false` | client: fail instead of falling back |

## Publishing artefacts

The image ships an empty `/data`. Artefacts are written into the volume, not
baked into the image — a 5 GB payload in the image means a 5 GB push on every
change.

Layout matters, because the directory prefix becomes the manifest `role` the
client filters on:

```
/data/
├── pylibs/      -> role "pylibs"
├── weights/     -> role "weights"
├── gguf/        -> role "gguf"   (model blobs; served by /gguf/, never unpacked)
├── pack/        -> role "pack"
└── manifest.json
```

`.scratch/` is excluded from the manifest on purpose: the upload spool lives
there, and folding an in-flight file into the manifest would change it on every
upload and defeat client caching.

## Cold start

Hashing a multi-gigabyte tree takes time proportional to its size, and the health
check has a 60 s start period to cover it. If your volume is large enough that
startup hashing becomes the bottleneck, publish `manifest.json` in CI instead and
set `EDGE_BUILD_MANIFEST=false`.

## Cost

CPU-only, ~16 GB RAM tier is ample for serving files. There is no GPU cost and no
Kaggle quota is consumed by running this — which is the point: the GPU quota is
the scarce resource, and this keeps more of it available for the work that
actually needs it.

## The controller half of this package

The Space is one half of `edge/`. The other half runs on the **Kaggle worker**
and is the master controller `NEW_WORKFLOW.MD` asks for. It is not reachable over
HTTP; it is a Go package the worker links or invokes.

| file | what it is |
|---|---|
| `ledger.go` | append-only JSONL job ledger, byte-compatible with `multitasker.py`'s |
| `queue.go` | bounded worker pool with the GPU-hotspot rule |

They are here, in the same package, because they are the same concern: this repo
is deciding what bytes get served and what work gets run, and both answers are
"content-addressed, verified, and resumable".

### Job states

```
queued ─┬─> preparing ─> prepared ─> dubbing ─> dubbed ─┬─> syncing ─> synced ─┐
        │                                                │                     │
        │                                                └─> uploading <───────┘
        │                                                      │
        ├──────────────────────────────────────────────────────┴──> done
        │
        └─> failed ─> queued            (the retry edge)
```

`failed` may be reached from anywhere and is the only state with an edge back
into the machine, because retrying is a normal operation here and not an
exception. A resume (`Ledger.Pending()`) returns everything that is **not**
`done` — including `failed`, so a chunk that died is retried instead of silently
dropped. One error is not the loss of a whole movie.

The vocabulary is `multitasker.py`'s, deliberately. Two ledgers over one job with
two state vocabularies is a tie-break decided by whichever process wrote last,
and that is not a thing to discover during a resume.

### The GPU hotspot rule

```go
type Job struct {
    ID      string
    Task    string   // "llm" | "asr" -- also the default hotspot
    Hotspot string   // overrides Task; "gpu0", "gpu1", ...
    Run     func(ctx context.Context, j Job) error
}
```

```
llm  ──┐          ┌── GPU 0   (llama-server, Homura-2B Q4_K_M)
       ├── run    ┤
asr  ──┘          └── GPU 1   (whisper-cli, large-v3-turbo)
```

* **two `llm` jobs are serialised** — one GPU, and one model load. Two
  llama-server processes each holding a 2B Q4_K_M on the same device is a VRAM
  overcommit, which shows up as an OOM kill halfway through a chunk rather than
  as anything that names its cause.
* **`llm` and `asr` run concurrently** — different GPUs. This is the notebook's
  existing "Homura on GPU 0, GPU 1 reserved for ASR/TTS" split, expressed as
  code instead of as a comment that a future edit can move.
* **an untagged job is not a free job** — it lands in one exclusive `default`
  lane. Letting it run alongside everything else would be a silent opt-out of
  the rule that keeps a run alive.

A hotspot is a named semaphore; capacity 1 is a mutex with a name and capacity
4 is four ASR workers on one large GPU. `NewQueue` refuses a capacity below 1,
because a hotspot nobody can enter is a deadlock that would otherwise be
discovered when every job hangs.

**One thing to know before changing it:** a worker holding a job while it waits
for a hotspot slot is a worker doing nothing. `Workers` should be at least the
number of distinct hotspots. The default is 4 for two lanes, which leaves slack.

### Tests

```bash
go test ./edge/          # HTTP, roster, ledger, queue
```

The concurrency tests are assertions, not demonstrations: `TestTwoLLMJobsAreSerialised`
checks a high-water mark of exactly 1 *and* a wall-clock floor, and
`TestLLMAndASRRunConcurrently` uses a rendezvous (both jobs must reach their body
before either is released) rather than a timing threshold, so it cannot pass by
being lucky on a fast machine.