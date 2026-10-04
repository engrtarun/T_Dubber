# SQLite index — how it fits in

`t_dubber.db` is an **index**, not a second source of truth. Video bytes live on
Telegram, media files live on disk, and the per-run `project.json` manifests
stay exactly where they were. What SQLite adds is the ability to *ask questions*
across all of that.

## Why it exists

Without it, every question is a folder crawl:

```
"Which movies are archived on Telegram but failed?"
"Which runs used Hindi and took longer than 2 hours?"
"What failed at the dataset-upload stage last week?"
```

With it:

```sql
SELECT p.title, p.status, a.channel, a.chunk_count
FROM projects p
LEFT JOIN telegram_archives a ON a.id = p.backup_archive_id
WHERE p.status = 'failed' AND a.id IS NOT NULL;

SELECT stage_name, AVG(duration_sec) FROM pipeline_stages
WHERE status = 'success' GROUP BY stage_name;

SELECT p.title FROM projects p JOIN pipeline_stages s
  ON s.project_id = p.id
WHERE s.stage_number = 3 AND s.status = 'failed';
```

## Tables

| Table | Holds |
|---|---|
| `projects` | one row per run: status, stage, kernel, target language |
| `telegram_archives` | every upload, keyed by content fingerprint + channel |
| `telegram_parts` | one row per uploaded part, with its SHA-256 |
| `media_metadata` | real title, uploader, runtime, source URL, thumbnail |
| `pipeline_stages` | per-stage outcome and duration |
| `quality_metrics` | one row per metric, so trends are queryable |
| `run_errors` | structured error log with stage and retry count |
| `voice_samples` / `speakers` | reusable voice references, per-run speaker mapping |
| `job_queue` | batch ordering, with `depends_on_id` for episode sequences |

## Concurrency

The Gradio UI reads from the main thread while an upload worker writes. Two
settings make that safe, and both are asserted by the tests:

- **WAL journal mode** — readers never block on the writer
- **15 s busy timeout** with an exponential-backoff retry on `BEGIN IMMEDIATE`

Writes go through `db._write`, which commits on success and **rolls back on any
exception, including `CancelledError`**, so a half-written project row is never
left visible.

## Rollout state

**Phase 1 (current).** The database is written *alongside* the existing JSON
files. JSON remains authoritative; nothing reads from SQLite to decide
behaviour except the Project History dropdown and the Drive overview, both of
which fall back to the old code path if the database is missing.

To verify nothing depends on it, delete `t_dubber.db` and start the app — it
re-seeds itself from `projects/` on first run.

**Phase 2 (next).** Flip reads to SQLite as primary, add stage writes inside
`pipeline.py`, then retire the `.tg_uploads/` journal reads.

**Phase 3.** Wire the `job_queue` to a batch UI, and build the quality dashboard
on `quality_metrics`.

## Commands

```powershell
python db.py         # schema version, table count, current stats
python db_seed.py    # one-time import of projects/ and .tg_uploads/
python test_db.py    # 8 tests: schema, upserts, rollback, concurrency, import
```

## What deliberately stays on disk

| Data | Why not SQLite |
|---|---|
| Video files, parts, output | Multi-gigabyte blobs; Telegram is the store |
| SRT / VTT subtitles | Read once by FFmpeg, never queried |
| Model weights | Gigabytes, immutable |
| `telegram_uploader_session.session` | Telethon owns this SQLite file exclusively |

That last row matters: two processes writing to Telethon's session corrupts it,
which is why `telegram_uploader.py` also takes an OS-level file lock before
opening a client.
