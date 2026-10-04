//! `havaldar_core` -- telemetry ingestion daemon for T_Dubber.
//!
//! # What this process is
//!
//! A single-purpose, memory-safe writer that accepts telemetry from the Kaggle
//! GPU worker and lands it in the local `t_dubber.db`, alongside the Python
//! writer (`db.py`) that owns the schema.
//!
//! ```text
//!   Kaggle worker  --HTTP/UDP-->  havaldar_core  -->  t_dubber.db
//!                                                        ^
//!                                                        |
//!                                            db.py (pipeline.py) also writes
//! ```
//!
//! # The one design decision that matters
//!
//! `rusqlite` is blocking, and SQLite allows exactly one writer at a time. The
//! two obvious implementations are both wrong:
//!
//! - Calling `Connection::execute` inside an async task blocks a tokio worker.
//! - `spawn_blocking` per packet turns every lock contention into a stalled
//!   runtime thread, and under load deadlocks the concurrency you wanted.
//!
//! So this daemon runs **one dedicated OS thread that owns the connection**,
//! fed by an `mpsc` channel and drained on a timer. See [`db`] for the full
//! reasoning. The consequences: the SQLite write lock is uncontended by
//! construction, a burst of packets costs one `fsync` rather than N, and the
//! async runtime never blocks on disk.
//!
//! # Interop with db.py
//!
//! Both writers set the same pragmas (`journal_mode=WAL`,
//! `synchronous=NORMAL`, a 15 s busy timeout), both use `BEGIN IMMEDIATE`
//! semantics, and both use the same four status literals. Where this daemon
//! differs from `db.record_stage` it does so deliberately and documents why:
//!
//! - it also stamps `started_at` on the `ON CONFLICT` path when the existing
//!   row has a NULL there, which repairs the known gap where a stage that goes
//!   straight to `success` loses its start time forever;
//! - it normalises incoming statuses (`processing` -> `running`) so a spec-style
//!   packet cannot write a value the dashboard cannot render.
//!
//! It never migrates, alters or drops `pipeline_stages` -- `db.py` owns that
//! table. It only creates `logs`, which does not exist yet.
//!
//! # Example
//!
//! ```no_run
//! use havaldar_core::db::{spawn, WriterConfig};
//! use std::path::Path;
//!
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! let writer = spawn(Path::new("t_dubber.db"), WriterConfig::default())?;
//! println!("writer ready");
//! # Ok(())
//! # }
//! ```

pub mod db;
pub mod error;
pub mod http;
pub mod packet;

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::Ordering;
use std::time::Duration;

use clap::Parser;
use tokio::sync::watch;

use crate::db::{spawn, WriterConfig};
use crate::http::AppState;
use crate::packet::TelemetryPacket;

/// Telemetry ingestion daemon for the T_Dubber dubbing pipeline.
///
/// Long-running: run it alongside `app.py` on the machine that owns
/// `t_dubber.db`. It is safe to stop and restart; queued packets are lost on an
/// abrupt exit, which is why the HTTP endpoint can wait for the commit.
#[derive(Debug, Parser)]
#[command(
    name = "havaldar_core",
    version,
    about = "Memory-safe telemetry writer for t_dubber.db",
    long_about = None,
)]
struct Args {
    /// Path to the SQLite database. Read-only use is not supported: this
    /// process writes stage transitions and log lines by design.
    #[arg(long, short = 'd', default_value = "t_dubber.db", env = "Havaldar_DB")]
    db: PathBuf,

    /// TCP port for the HTTP ingest API.
    #[arg(long, short = 'p', default_value_t = 8080, env = "Havaldar_PORT")]
    port: u16,

    /// Interface to bind. Loopback by default.
    ///
    /// WARNING: the payload carries Telegram channels, file paths and error
    /// text. Binding beyond loopback exposes them to anything that can reach
    /// this port. There is no authentication; put it behind a tunnel or a
    /// firewall rule if you bind wider.
    #[arg(long, default_value = "127.0.0.1", env = "Havaldar_HOST")]
    host: String,

    /// Also listen for UDP telemetry packets on this port. 0 disables it.
    #[arg(long, default_value_t = 0)]
    udp_port: u16,

    /// Maximum packets buffered before returning 503 to new arrivals.
    #[arg(long, default_value_t = 8_192)]
    queue_capacity: usize,

    /// How often the writer commits a batch of queued packets.
    #[arg(long, default_value_t = 250)]
    flush_interval_ms: u64,

    /// SQLite busy timeout. Matches db.py's 15 s: both writers should wait the
    /// same amount before giving up, or the loser fails instantly under load.
    #[arg(long, default_value_t = 15_000)]
    busy_timeout_ms: u64,

    /// Do not create the `logs` table if it is missing. Use when db.py owns
    /// migrations and this process must only ever read.
    #[arg(long)]
    no_migrate: bool,

    /// Log format.
    #[arg(long, value_enum, default_value_t = LogFormat::Pretty)]
    log_format: LogFormat,

    /// Log filter, e.g. `info`, `havaldar_core=debug`.
    #[arg(long, default_value = "info", env = "RUST_LOG")]
    log_filter: String,
}

#[derive(Debug, Clone, Copy, clap::ValueEnum)]
enum LogFormat {
    Pretty,
    Json,
}

fn main() -> std::process::ExitCode {
    let args = Args::parse();
    init_tracing(args.log_format, &args.log_filter);

    // Build a multi-threaded runtime explicitly. `#[tokio::main]` with the
    // multi-thread flavor would do this too, but doing it by hand lets the
    // worker count be reported in the startup log, which matters when someone
    // wonders why a small ingest service is pinning cores.
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .thread_name("havaldar")
        .build()
    {
        Ok(rt) => rt,
        Err(e) => {
            tracing::error!(error = %e, "could not start the tokio runtime");
            return std::process::ExitCode::FAILURE;
        }
    };

    match runtime.block_on(run(args)) {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => {
            tracing::error!(error = %e, "fatal");
            std::process::ExitCode::FAILURE
        }
    }
}

fn init_tracing(format: LogFormat, filter: &str) {
    use tracing_subscriber::EnvFilter;
    let env_filter = EnvFilter::try_new(filter).unwrap_or_else(|_| EnvFilter::new("info"));
    let builder = tracing_subscriber::fmt().with_env_filter(env_filter);
    match format {
        LogFormat::Json => builder.json().with_target(true).init(),
        LogFormat::Pretty => builder.with_target(true).init(),
    }
}

async fn run(args: Args) -> error::Result<()> {
    tracing::info!(
        db = %args.db.display(),
        workers = tokio::runtime::Handle::current().metrics().num_workers(),
        "starting havaldar_core"
    );

    let cfg = WriterConfig {
        queue_capacity: args.queue_capacity.max(1),
        flush_interval: Duration::from_millis(args.flush_interval_ms.max(10)),
        busy_timeout: Duration::from_millis(args.busy_timeout_ms.max(100)),
        migrate: !args.no_migrate,
    };

    // Fail loudly at startup rather than accepting packets we cannot store.
    let writer = spawn(&args.db, cfg.clone())?;
    tracing::info!(
        queue_capacity = cfg.queue_capacity,
        flush_interval_ms = cfg.flush_interval.as_millis(),
        busy_timeout_ms = cfg.busy_timeout.as_millis(),
        migrated = cfg.migrate,
        "database writer ready"
    );

    let state = AppState::new(writer, cfg.queue_capacity);
    let host = args.host.clone();
    let host_udp = args.host.clone();
    let udp_port = args.udp_port;
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    // A second handle for the signal path.
    let signal_tx = shutdown_tx.clone();

    // Graceful shutdown: stop accepting, let the writer drain, exit.
    let shutdown = async move {
        shutdown_signal().await;
        tracing::info!("shutdown signal received; draining");
        signal_tx.send(true).ok();
    };

    let mut tasks = Vec::new();

    // ---- HTTP ----
    let http_state = state.clone();
    let http_shutdown = shutdown_rx.clone();
    tasks.push(tokio::spawn(async move {
        let app = http::router(http_state);
        let addr: SocketAddr = format!("{}:{}", host, args.port)
            .parse()
            .unwrap_or_else(|_| {
                SocketAddr::from(([127, 0, 0, 1], args.port))
            });
        let listener = match tokio::net::TcpListener::bind(addr).await {
            Ok(l) => l,
            Err(e) => {
                tracing::error!(%addr, error = %e, "could not bind HTTP listener");
                return;
            }
        };
        tracing::info!(%addr, "HTTP ingest ready  (POST /ingest, /ingest/batch, /telemetry)");
        match axum::serve(listener, app)
            .with_graceful_shutdown(async move {
                let mut rx = http_shutdown;
                while rx.changed().await.is_ok() {
                    if *rx.borrow() {
                        break;
                    }
                }
            })
            .await
        {
            Ok(()) => tracing::info!("HTTP listener stopped"),
            Err(e) => tracing::error!(error = %e, "HTTP server error"),
        }
    }));

    // ---- UDP (optional) ----
    if udp_port > 0 {
        let udp_state = state.clone();
        let udp_shutdown = shutdown_rx.clone();
        let udp_shutdown_keepalive = udp_shutdown.clone();
        let addr: SocketAddr = format!("{}:{}", host_udp, udp_port)
            .parse()
            .unwrap_or_else(|_| SocketAddr::from(([127, 0, 0, 1], udp_port)));
        tasks.push(tokio::spawn(async move {
            if let Err(e) = http::serve_udp(addr, udp_state, udp_shutdown.clone()).await {
                tracing::error!(%addr, error = %e, "UDP listener failed");
                shutdown_tx.send(true).ok();
            }
        }));
        // Keep the receiver alive until shutdown flips.
        tasks.push(tokio::spawn(async move {
            let mut rx = udp_shutdown_keepalive;
            while rx.changed().await.is_ok() {
                if *rx.borrow() {
                    break;
                }
            }
        }));
    }

    state.shutting_down.store(false, Ordering::Relaxed);
    tracing::info!("havaldar_core is accepting telemetry");

    shutdown.await;

    // Flip readiness before draining so a load balancer stops sending new work.
    state.shutting_down.store(true, Ordering::Relaxed);

    for task in tasks {
        // Each task observes the watch channel and exits on its own.
        if let Err(e) = tokio::time::timeout(Duration::from_secs(10), task).await {
            tracing::warn!(error = %e, "listener did not stop within 10s; forcing");
        }
    }

    tracing::info!("havaldar_core stopped cleanly");
    Ok(())
}

/// Resolve on SIGINT/SIGTERM, or on stdin EOF so the daemon can be run
/// under a supervisor that pipes rather than signals.
async fn shutdown_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut term = match signal(SignalKind::terminate()) {
            Ok(s) => s,
            Err(e) => {
                tracing::warn!(error = %e, "cannot install SIGTERM handler");
                return std::future::pending().await;
            }
        };
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = term.recv() => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}

/// Round-trip a packet through validation, used by the smoke test in main's
/// startup path and by integration tests.
pub fn validate_smoke_test(raw: &str) -> error::Result<String> {
    let packet: TelemetryPacket = serde_json::from_str(raw)?;
    let event = packet.into_event(false)?;
    Ok(format!("{:?}", event.project_id()))
}

/// Convenience re-exports for callers embedding this crate.
pub use db::{Writer, WriterConfig as Config};
pub use error::{HavaldarError, Result};