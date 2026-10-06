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

`Range` exists because Kaggle sessions get reaped mid-download; a client resumes
from the byte it reached. `ETag` is the content digest, so a client that
already has a file can revalidate in one round trip.

The other endpoints are described under "Endpoints" below; this table is the
short version.

## What it is not

- Not an orchestrator. There is no queue and no state machine.
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