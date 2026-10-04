# havaldar_core

Telemetry ingestion daemon for T_Dubber. Accepts stage transitions and log
lines from the Kaggle GPU worker and writes them to the local `t_dubber.db`,
alongside the existing Python writer.

```
  Kaggle worker ──HTTP (primary) / UDP (progress)──▶ havaldar_core ──▶ t_dubber.db
                                                                      ▲
                                                            db.py also writes
```

## Build

```bash
cd havaldar_core
cargo build --release          # binary is ./target/release/havaldar_core
cargo test                     # unit + integration tests against real SQLite
```

Static Linux build for shipping as a Kaggle Dataset artifact:

```bash
rustup target add x86_64-unknown-linux-musl
cargo build --release --target x86_64-unknown-linux-musl
```

## Run

```bash
havaldar_core --db t_dubber.db --port 8080
havaldar_core --db t_dubber.db --port 8080 --udp-port 8081 --log-format json
```

| Flag | Default | Notes |
|---|---|---|
| `--db` | `t_dubber.db` | env `Havaldar_DB` |
| `--port` | `8080` | env `Havaldar_PORT` |
| `--host` | `127.0.0.1` | loopback by default, see Security |
| `--udp-port` | `0` (off) | progress ticks only |
| `--queue-capacity` | `8192` | beyond this, 503 |
| `--flush-interval-ms` | `250` | batch commit window |
| `--busy-timeout-ms` | `15000` | matches `db.py` |
| `--no-migrate` | off | never create tables |
| `--log-format` | `pretty` | or `json` |
| `--log-filter` | `info` | env `RUST_LOG` |

## Endpoints

| Route | Method | Purpose |
|---|---|---|
| `/ingest` | POST | one packet; `?wait=1` blocks for the commit |
| `/ingest/batch` | POST | array of up to 512 packets |
| `/telemetry` | POST | alias of `/ingest` |
| `/health`, `/healthz` | GET | liveness; **never touches the database** |
| `/ready` | GET | readiness; 503 while draining |
| `/api/stats` | GET | counters |
| `/api/recent` | GET | tail of accepted events |

### Packet

```json
{ "project_id": "clip-4f9a2c1d-8e31b0",
  "stage": 4,
  "status": "running",
  "message": "TTS encoding...",
  "duration_sec": null }
```

A packet with a `status` (or a `stage`) is a **stage transition**; otherwise it
is a **log line** and needs a `message`. Optional for logs: `level`
(defaults to `info`).

`status` accepts the spellings a worker naturally sends and normalises them to
the four values `db.py` writes:

| Sent | Stored |
|---|---|
| `running`, `processing`, `in_progress`, `started`, `working` | `running` |
| `success`, `ok`, `done`, `complete`, `finished`, `passed` | `success` |
| `failed`, `error`, `failure`, `crashed`, `timeout`, `fault` | `failed` |
| `skipped`, `bypassed` | `skipped` |

Anything else is a `400`. This is deliberate: writing `"processing"` verbatim
would create a row that `dashboard_server.py` cannot render.

### Status codes

| Code | Meaning | Caller should |
|---|---|---|
| `202` | queued, not yet durable | continue |
| `200` | committed (with `?wait=1`) | continue |
| `400` | malformed packet | **do not retry**, fix the sender |
| `409` | unknown `project_id` (FK) | **do not retry**, create the project first |
| `503` | queue full / DB busy | retry with backoff (`Retry-After: 1`) |
| `500` | genuine fault | retry with backoff |

The 400/409 vs 503 split matters: a worker that retries a `400` forever wastes
its whole run, and one that treats a `503` as fatal gives up on a 200 ms hiccup.

## UDP

Enabled with `--udp-port`. Progress ticks only — a lost datagram is acceptable
for "still working", unacceptable for a terminal transition. A `success`/`failed`
arriving over UDP is recorded as a `warn` log line rather than applied, telling
the sender to resend over HTTP.

## Design notes

**One writer thread.** `rusqlite` is blocking and SQLite allows one writer.
Running it on an async task blocks the runtime; `spawn_blocking` per packet
turns every lock contention into a stalled worker. So a single OS thread owns
the connection, fed by a `sync_channel`, drained with `recv_timeout`. The
thread has no tokio reactor, which is why it uses `std::sync::mpsc` rather than
`tokio::sync::mpsc` — the latter would panic without a timer driver.

**Batching.** A burst of packets costs one `fsync`, not N.

**Interop with `db.py`.** Both set `journal_mode=WAL`, `synchronous=NORMAL` and
a 15 s busy timeout, and both use the four status literals above. Two deliberate
differences, both fixing known `db.py` gaps rather than diverging from it:

- `started_at` is stamped on the `ON CONFLICT` path when the existing row has a
  NULL there. `db.record_stage`'s `ON CONFLICT` clause does not update
  `started_at`, so a stage that goes straight to `success` keeps a NULL start
  time forever.
- `current_stage` is advanced with `MAX(current_stage, ?)` exactly as
  `db.py` does, so a late low-numbered packet cannot rewind the pointer.

**Migrations are additive only.** `pipeline_stages` is owned by `db.py`; this
daemon never alters or drops it, and asserts its shape at startup so a
mismatch fails loudly instead of producing confusing constraint errors. The
`logs` table (which does not exist yet) is created here rather than by editing
`db.py`, so no Python file is touched.

**No panics in request paths.** `unsafe_code = "forbid"` is set in Cargo.toml,
so the memory-safety claim is compiler-enforced. `panic = "abort"` in release
keeps a panic from unwinding into the runtime. `.unwrap()` appears only in
tests.

## Security

No authentication or TLS. This is a **loopback-only** service by default and
should stay that way. The payload carries Telegram channels, file paths and
error text. If you must bind wider, put it behind a tunnel or firewall rule —
and note there is no per-project authorisation, so any reachable client can
write telemetry for any project.

## Testing

`cargo test` runs unit tests (status normalisation, timestamp formatting,
UTF-8-safe truncation) and integration tests against a real SQLite file:
WAL/NORMAL pragmas, `started_at` preservation across the upsert, the FK
rejection path, `current_stage` monotonicity, and 80 concurrent writes
collapsing to the correct number of rows.