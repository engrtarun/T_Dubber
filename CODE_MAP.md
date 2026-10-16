# CODE_MAP.md — T_Dubber: read this first

> **Naya agent? Ye padh, phir code chhoo.**
> Rules [`AGENTS.md`](AGENTS.md) me hain. Ye file batata hai **code kahan hai**.

**What this is:** a Python + Go + Rust + C++ video dubbing/translation pipeline.
Downloads a video, translates/dubs it (on a Kaggle GPU worker), assembles the
result, and uploads it to Telegram as cloud storage. A local SQLite database
tracks every project and every stage.

---

## 1. Ten-second orientation

| Kya hai | Kahan hai |
|---|---|
| Main UI (Gradio) + orchestrator | `app.py` |
| Everything is stored | `db.py` (SQLite, WAL) |
| Telegram upload / cloud storage | `telegram_uploader.py` |
| Kaggle GPU worker | `multitasker.py`, `pipeline.py` |
| Native speed paths (Rust/C++/Go) | `stitcher/`, `subtitle_forge/`, `cpp_accelerator/`, `tgup.exe` |

**Data flows:** source URL → `link_resolver.py` → ffmpeg → chunks
(`chunking.py`) → Telegram (`telegram_uploader.py`) → ledger (`db.py`).
The heavy GPU work happens **off-machine, on Kaggle**, not here.

---

## 2. Where things live — ours vs not ours

**Only the top two rows are ours. Do not lint, do not refactor, do not "fix"
anything below the line.**

| Path | What it is |
|---|---|
| `*.py` at repo root | **First-party Python.** 56 files, ~1.1 MB. This is the project. |
| `test_*.py`, `multitasker_test.py`, `conftest.py`, `testkit.py` | **Tests** (root, not in `tests/`). |
| `tests/` | Only 5 subtitle_forge tests + Rust/C++ bridge tests. |
| `huggingface/` | HF/GGUF model store. First-party but a sub-package. |
| `lip_sync/` | Lip-sync. First-party sub-package. |
| `go_planner.py`, `tgup_bridge.py` | Go `tgup.exe` adapters. |
| `stitcher/`, `subtitle_forge/`, `havaldar_core/`, `cpp_accelerator/`, `tts_rust/` | **Rust + C++ native.** Not Python. |
| `edge/`, `cmd/` | Go HTTP service (Kaggle model fetcher). |
| ─────────────── | ─────────── **EVERYTHING BELOW IS NOT OURS** ─────────── |
| `mazinger/`, `Index-Translate/`, `references/*`, `kotaemon-gradio-theme/`, `ui-buttons/`, `css-loaders/` | **9 git submodules**, third-party. `ruff --fix` on them dirties the submodule permanently. |
| `projects/` | **Runtime data, not source.** Each folder holds the `.mp4` output, `project.json`, `kernel_safe/`, `pylibs/` (vendored Pillow), and `dataset_safe/` — a **stale snapshot of this repo's own source**, one of which had already drifted. Ignored since 2026-10-16; nothing was deleted. |
| `archive/`, `drive/`, `deepclonewebsite/`, `kaggle_test_dataset/`, `Kaggle Tests Case/` | Archived work and unrelated side projects. |

> The submodule list is in `.gitmodules`; the exclusion list is in
> `pyproject.toml`, `pytest.ini` and `.pre-commit-config.yaml`.
> **All three must stay in step.** A gate that walks a submodule dirties it;
> a gate that walks `projects/` checks the same file 600 times.

> **Concurrency:** this repo is worked on by several agents at once. If a file
> you are about to edit has been modified in the last few minutes, someone else
> is in it — read the diff and coordinate via `AGENTS_MAP.md` before touching
> it. Do not "fix" an in-flight edit; that is how two agents overwrite each
> other and lose work.

---

## 3. How the modules depend on each other

```
app.py            facade: wires everything together, 2815 lines
  |
  +-- pipeline.py           Kaggle kernel launch + poll      (needs havaldar, db)
  +-- telegram_uploader.py  upload/download/restore          (needs go_planner, auto_tuner)
  +-- link_resolver.py      URL -> local file / stream        (needs telegram_uploader)
  +-- direct_archive.py     zero-local-byte archive           (needs link_resolver, db, go_planner)
  +-- db.py                 SQLite storage                   (leaf)
  +-- ui_static.py          Gradio HTML/JS assets             (leaf)

chunking.py       adaptive chunker  (independent -- NOT wired into the
chunk_ledger.py   SQL index over    telegram_uploader; used by tests + notebook)
```

**Islands** — import nothing of ours, so they are the safest place to start:
`db.py`, `multitasker.py`, `dashboard_server.py`, `telethon_session.py`,
`ui_static.py`, `space_sweeper.py`, `auto.py`.

**Leaf-to-root order** for a typing/refactor pass: islands → adapters
(`tgup_bridge`, `go_planner`, `havaldar`) → services → `app.py` last.

---

## 4. Entry points

**Run as a script** (has `if __name__ == "__main__"`): `app.py`, `db.py`,
`pipeline.py`*, `telegram_uploader.py`, `link_resolver.py`,
`dashboard_server.py`, `multitasker.py`, `chop_drop.py`, `tgup_bridge.py`,
`go_planner.py`, `space_sweeper.py`, `auto.py`, `doctor.py`,
`wire_multitasker.py`, `telethon_session.py`, `login_once.py`,
`e2e_upload.py`, `db_seed.py`, `patch_notebook_p0.py`, `prune_*.py`,
`setup_config.py`.

**Libraries** (import these, never run them): `chunking.py`, `chunk_ledger.py`,
`havaldar.py`, `direct_archive.py`, `ui_static.py`, `auto_tuner.py`.

---

## 5. Traps — do not get caught by these

### 5.1 Three files execute real side effects on import

Verified 2026-10-16. There is no `if __name__ == "__main__"` guard, so merely
**importing** them runs the code:

| File | What happens on import |
|---|---|
| `remove_two_fixture_rows.py` | `db.connect()` then **`DELETE` from the production database.** It makes a backup and aborts unless it finds exactly 2 rows — but it still runs. |
| `test_passwords.py` | `asyncio.run(main())` — **opens a real Telegram session** with live credentials. |
| `test_upload_manual.py` | **Performs a real Telegram upload** of a local video. |
| `_probe_db.py` | Read-only queries and prints. Harmless, just noisy. |

The last two match `test_*.py`, so a **bare `pytest` run that ignores
`pytest.ini`'s `testpaths` would collect them and dial Telegram.** Always run
`python -m pytest` from the repo root so `testpaths` applies.

Consequence for tooling: **never write a tool that imports modules to inspect
them.** Use `ast.parse`, or run in a subprocess.

### 5.2 Tests run two ways

Every test must work both standalone and under pytest:

```
python test_db.py              # unittest, no plugins, no third-party deps
python -m pytest test_db.py    # fixtures, autouse isolation
```

A test that passes only under pytest is broken — several scripts run on Kaggle
where pytest does not exist.

### 5.3 No test may touch production data

`conftest.py`'s autouse fixture redirects `db.DB_PATH` **and**
`telegram_uploader.STATE_DIR` to scratch for **every** test, whatever it asked
for. Measured reason: `test_p0_direct.py` and `test_flow_order.py` each passed
alone and left `projects` unchanged, yet run together they wrote **4 rows into
production** — because one restored the path while the other still believed it
owned it. **Do not add a test that bypasses that fixture.**

Verify with `COUNT(*)`, never file mtime — SQLite runs in WAL mode, so writes
land in `-wal` and mtime only moves on checkpoint.

### 5.4 Telegram credentials

Committed to git history in 6 commits. **Source is clean, rotation is still
outstanding.** See [`SECURITY.md`](SECURITY.md). Never hardcode one again.

### 5.5 `projects/*/dataset_safe/` is not source

Those files look like the first-party modules and some still share names with
them. They are **stale snapshots**, one `multitasker.py` no longer matches the
root. Read the root file, never the copy.

---

## 6. Test library — use it, do not re-invent it

[`testkit.py`](testkit.py) holds the shared test arrangement, because
15 files hand-rolled a temp dir, 10 open-coded a sha256, 7 repeated the same
fake-video line, and 3 hand-wrote an HTTP handler.

Under pytest, ask for the fixture:

```python
def test_it(scratch_workspace, fake_movie, tgup_capture, sha256_of):
    source = fake_movie(size=10_000)
    tgup_capture(["upload", "--file", str(source)])
    assert tgup_capture.flag("--file") == str(source)
```

Standalone (`python test_x.py`), construct directly:

```python
import testkit
ws = testkit.ScratchWorkspace()
source = testkit.fake_media(ws.path)
```

| Helper | Use for |
|---|---|
| `ScratchWorkspace` | temp dir that always cleans up |
| `fake_media` | the fake video file (name/size matter — chunk names derive from them) |
| `sha256_of` | digest bytes / str / **file** |
| `CapturedCommand` | fake `tgup_bridge.run_command`; `.flag()`, `.called_with()`, `.stdin_text` |
| `EnvGuard` | set env vars, guaranteed restore |
| `serve_bytes` / `stop_server` | Range-capable HTTP server on a free port |
| `tg_link` / `tg_link_roundtrip` | links asserted against the **real** functions |
| `chunk_manifest`, `manifest_records`, `manifest_chunk_size` | real manifests. **Records are 1-based** (`{1,2,3}`). |
| `db_row_count` | counts at the *current* `db.DB_PATH` |

Rule: three or more callers before a helper belongs here. Assertions stay in
the tests — a helper that asserts produces failure messages that point at the
library instead of the bug.

---

## 7. Getting oriented in an unfamiliar module

```bash
# What does it do, and how big is it?
head -40 app.py

# Callers
git grep -n "import app" -- "*.py"

# What does the ruff gate say about it right now?
python -m ruff check --no-cache app.py

# What do its tests cover?
python -m pytest test_p0_direct.py -k credentials -v
```

Three more: `doctor.py` audits the local environment (binaries, deps, ffmpeg,
config); `AGENTS_MAP.md` records who is working on what and carries post-mortems
from past sessions; and `python -m pytest` must be green before you claim done.