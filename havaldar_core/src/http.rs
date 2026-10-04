//! The ingest surface: HTTP (axum) and UDP.
//!
//! # Why two transports
//!
//! - **HTTP** is the primary. It is request/response, so the worker gets a real
//!   acknowledgement: a 200 means the row is committed, a 409 means the project
//!   is unknown, a 503 means back off. That is what lets the Kaggle side
//!   implement correct retry with exponential backoff instead of fire-and-forget.
//! - **UDP** is the fast path for high-rate progress ticks. A worker emitting a
//!   "still working" heartbeat every 100 ms does not want 10 connections per
//!   second. UDP trades delivery for latency, which is the right trade for
//!   intermediate progress and the wrong one for terminal transitions -- so the
//!   rule is: **terminal statuses go over HTTP, progress over UDP.** Anything
//!   UDP receives is treated as non-terminal; a UDP "failed" packet is upgraded
//!   and logged rather than trusted, because a dropped retry of a terminal
//!   event should not be silently ignored forever.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use axum::extract::{DefaultBodyLimit, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use tokio::net::UdpSocket;
use tokio::sync::broadcast;

use crate::db::{self, Writer};
use crate::error::{HavaldarError, Result};
use crate::packet::TelemetryPacket;

/// Telemetry packets per HTTP request.
///
/// `TelemetryPacket` is a handful of optional strings; 64 KiB is far more than a
/// legitimate packet needs and stops an unauthenticated caller from streaming
/// megabytes into memory.
const MAX_BODY_BYTES: usize = 64 * 1024;

/// Shared application state.
#[derive(Clone)]
pub struct AppState {
    pub writer: Writer,
    pub queue_capacity: usize,
    /// Ring buffer of recent outcomes, for `/api/recent` and the UDP path.
    pub recent: broadcast::Sender<RecentEvent>,
    pub started: std::time::Instant,
    pub shutting_down: Arc<AtomicBool>,
}

/// One recently accepted event, broadcast to observers.
#[derive(Debug, Clone, serde::Serialize)]
pub struct RecentEvent {
    pub project_id: String,
    pub kind: &'static str,
    pub stage: Option<i64>,
    pub at: String,
}

impl AppState {
    pub fn new(writer: Writer, queue_capacity: usize) -> Self {
        // A small ring: enough for a dashboard tail, bounded so a forgetful
        // subscriber cannot grow memory without limit.
        let (recent, _) = broadcast::channel(1024);
        Self {
            writer,
            queue_capacity,
            recent,
            started: std::time::Instant::now(),
            shutting_down: Arc::new(AtomicBool::new(false)),
        }
    }
}

/// Convert a domain error into an HTTP response.
///
/// The mapping is deliberate: a rejected packet is the *caller's* problem (409
/// or 400), a busy database is *ours* but transient (503 with Retry-After), and
/// anything else is a genuine fault (500). Getting this wrong either hides bugs
/// or makes the worker retry things that can never succeed.
struct ApiError(HavaldarError);

impl From<HavaldarError> for ApiError {
    fn from(e: HavaldarError) -> Self {
        Self(e)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, kind) = match &self.0 {
            HavaldarError::Validation { .. } => (StatusCode::BAD_REQUEST, "validation_error"),
            HavaldarError::Overloaded { .. } => (StatusCode::SERVICE_UNAVAILABLE, "overloaded"),
            HavaldarError::WriterGone => (StatusCode::SERVICE_UNAVAILABLE, "writer_unavailable"),
            e if e.is_constraint_violation() => (StatusCode::CONFLICT, "unknown_project"),
            e if e.is_busy() => (StatusCode::SERVICE_UNAVAILABLE, "database_busy"),
            _ => (StatusCode::INTERNAL_SERVER_ERROR, "internal_error"),
        };

        let mut body = serde_json::json!({
            "ok": false,
            "error": kind,
            "detail": self.0.to_string(),
        });
        // Tell a retrying client how long to wait. Without this, an exponential
        // backoff implementation has to guess.
        if status == StatusCode::SERVICE_UNAVAILABLE {
            body["retry_after_sec"] = serde_json::json!(1);
        }

        let mut response = (status, Json(body)).into_response();
        if status == StatusCode::SERVICE_UNAVAILABLE {
            response.headers_mut().insert(
                "retry-after",
                axum::http::HeaderValue::from_static("1"),
            );
        }
        response
    }
}

/// `POST /ingest` -- the primary ingest endpoint.
///
/// Fire-and-forget by default (`?wait=1` waits for the commit). The default
/// matters: the worker should not block its dub loop on our disk.
async fn ingest(
    State(state): State<AppState>,
    Query(params): Query<std::collections::HashMap<String, String>>,
    Json(packet): Json<TelemetryPacket>,
) -> std::result::Result<(StatusCode, Json<serde_json::Value>), ApiError> {
    if state.shutting_down.load(Ordering::Relaxed) {
        return Err(ApiError(HavaldarError::WriterGone));
    }

    // `seen_running=false` here means: treat this as the first running packet.
    // The writer thread owns the authoritative set; a duplicate "running" is
    // idempotent under the upsert, so a conservative answer is safe.
    let event = packet.into_event(false)?;

    let (project_id, kind, stage) = match &event {
        crate::packet::TelemetryEvent::Stage(e) => {
            (e.project_id.clone(), "stage", Some(e.stage))
        }
        crate::packet::TelemetryEvent::Log(e) => (e.project_id.clone(), "log", e.stage),
    };

    let wait = params.get("wait").map(|v| v == "1" || v == "true").unwrap_or(false);

    if wait {
        let outcome = state
            .writer
            .submit_and_wait(
                event,
                state.queue_capacity,
                Duration::from_millis(5_000),
            )
            .await
            .map_err(ApiError)?;
        if let Some(reason) = outcome.rejected {
            return Err(ApiError(HavaldarError::Config(reason)));
        }
        let _ = state.recent.send(RecentEvent {
            project_id,
            kind,
            stage,
            at: db::now_iso8601(),
        });
        return Ok((
            StatusCode::OK,
            Json(serde_json::json!({"ok": true, "applied": true, "durable": true})),
        ));
    }

    state
        .writer
        .submit(event, state.queue_capacity)
        .await
        .map_err(ApiError)?;
    let _ = state.recent.send(RecentEvent {
        project_id,
        kind,
        stage,
        at: db::now_iso8601(),
    });
    Ok((
        StatusCode::ACCEPTED,
        Json(serde_json::json!({"ok": true, "accepted": true, "durable": false})),
    ))
}

/// Batch endpoint: an array of packets in one transaction's worth of queueing.
async fn ingest_batch(
    State(state): State<AppState>,
    Json(packets): Json<Vec<TelemetryPacket>>,
) -> std::result::Result<(StatusCode, Json<serde_json::Value>), ApiError> {
    const MAX_BATCH: usize = 512;
    if packets.is_empty() {
        return Err(ApiError(HavaldarError::invalid("body", "must not be empty")));
    }
    if packets.len() > MAX_BATCH {
        return Err(ApiError(HavaldarError::invalid(
            "body",
            format!("at most {MAX_BATCH} packets per request"),
        )));
    }

    // Validate the whole batch before queueing any of it. A partially accepted
    // batch is the worst outcome: the caller cannot tell which half landed.
    let mut events = Vec::with_capacity(packets.len());
    for (i, p) in packets.into_iter().enumerate() {
        let ev = p.into_event(false).map_err(|e| match e {
            HavaldarError::Validation { field, detail } => ApiError(HavaldarError::Validation {
                field,
                detail: format!("packet[{i}]: {detail}"),
            }),
            other => ApiError(other),
        })?;
        events.push(ev);
    }

    for ev in events {
        state
            .writer
            .submit(ev, state.queue_capacity)
            .await
            .map_err(ApiError)?;
    }
    Ok((
        StatusCode::ACCEPTED,
        Json(serde_json::json!({"ok": true, "accepted": events.len()})),
    ))
}

/// `GET /health` -- liveness. Never touches the database.
///
/// Deliberately separate from `/ready`: a liveness probe that touches SQLite
/// will get the process killed during exactly the contention it was meant to
/// report.
async fn health(State(state): State<AppState>) -> impl IntoResponse {
    (
        StatusCode::OK,
        Json(serde_json::json!({
            "ok": true,
            "uptime_sec": state.started.elapsed().as_secs(),
            "shutting_down": state.shutting_down.load(Ordering::Relaxed),
        })),
    )
}

/// `GET /ready` -- readiness. Reports the writer channel state.
async fn ready(State(state): State<AppState>) -> Response {
    let shutting_down = state.shutting_down.load(Ordering::Relaxed);
    let status = if shutting_down {
        StatusCode::SERVICE_UNAVAILABLE
    } else {
        StatusCode::OK
    };
    (
        status,
        Json(serde_json::json!({
            "ready": !shutting_down,
            "shutting_down": shutting_down,
        })),
    )
        .into_response()
}

/// `GET /api/stats` -- counters for monitoring.
async fn stats(State(state): State<AppState>) -> impl IntoResponse {
    Json(serde_json::json!({
        "uptime_sec": state.started.elapsed().as_secs(),
        "queue_capacity": state.queue_capacity,
        "shutting_down": state.shutting_down.load(Ordering::Relaxed),
    }))
}

/// `GET /api/recent` -- tail of accepted events (SSE-free, polled).
async fn recent(State(state): State<AppState>) -> Response {
    let mut out = Vec::new();
    let mut rx = state.recent.subscribe();
    // Drain what is buffered without blocking.
    while let Ok(ev) = rx.try_recv() {
        out.push(ev);
    }
    (StatusCode::OK, Json(serde_json::json!({"events": out}))).into_response()
}

/// Build the HTTP router.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/ingest", post(ingest))
        .route("/ingest/batch", post(ingest_batch))
        // Aliases so the worker can use whichever reads better at the call site.
        .route("/api/ingest", post(ingest))
        .route("/telemetry", post(ingest))
        .route("/health", get(health))
        .route("/healthz", get(health))
        .route("/ready", get(ready))
        .route("/api/stats", get(stats))
        .route("/api/recent", get(recent))
        .fallback(|| async { (StatusCode::NOT_FOUND, "no such route") })
        .layer(DefaultBodyLimit::max(MAX_BODY_BYTES))
        .with_state(state)
}

/// Serve UDP on `addr` until `shutdown` flips.
///
/// Each datagram is one JSON packet. This runs on the tokio runtime, but it does
/// no database work: it validates and hands off to the writer, so a slow disk
/// cannot stall the receive loop.
pub async fn serve_udp(addr: SocketAddr, state: AppState, mut shutdown: tokio::sync::watch::Receiver<bool>) -> Result<()> {
    let socket = UdpSocket::bind(addr).await?;
    let local = socket.local_addr()?;
    tracing::info!(%local, "UDP telemetry listener ready");

    // 64 KiB is the largest datagram we will read; anything larger is truncated
    // by the network layer anyway.
    let mut buf = vec![0u8; 65_536];

    loop {
        tokio::select! {
            biased;
            _ = shutdown.changed() => {
                if *shutdown.borrow() {
                    tracing::info!("UDP listener draining");
                    return Ok(());
                }
            }
            recv = socket.recv_from(&mut buf) => {
                let (len, peer) = match recv {
                    Ok(v) => v,
                    Err(e) => {
                        // One bad datagram must not kill the listener.
                        tracing::warn!(error = %e, "UDP recv failed");
                        continue;
                    }
                };
                if let Err(e) = handle_udp_datagram(&buf[..len], peer, &state).await {
                    tracing::warn!(%peer, error = %e, "UDP packet rejected");
                }
            }
        }
    }
}

async fn handle_udp_datagram(
    bytes: &[u8],
    peer: SocketAddr,
    state: &AppState,
) -> Result<()> {
    let packet: TelemetryPacket = serde_json::from_slice(bytes)?;

    // UDP is for progress, not for terminal state. A "failed" that arrives
    // here cannot be trusted to be the last word, so it is recorded as a log
    // line rather than as a terminal stage transition.
    let is_terminal = packet
        .status
        .as_deref()
        .map(|s| {
            let lower = s.trim().to_ascii_lowercase();
            lower == "failed" || lower == "error" || lower == "success" || lower == "done"
        })
        .unwrap_or(false);

    let event = if is_terminal {
        crate::packet::TelemetryEvent::Log(crate::packet::LogEvent {
            project_id: packet
                .project_id
                .clone()
                .ok_or_else(|| HavaldarError::invalid("project_id", "required"))?,
            stage: packet.stage,
            level: "warn".into(),
            message: format!(
                "terminal status {:?} seen over UDP; not applied as a stage \
                 transition -- resend over HTTP to commit it",
                packet.status.unwrap_or_default()
            ),
        })
    } else {
        packet.into_event(false)?
    };

    let (project_id, kind, stage) = match &event {
        crate::packet::TelemetryEvent::Stage(e) => {
            (e.project_id.clone(), "stage", Some(e.stage))
        }
        crate::packet::TelemetryEvent::Log(e) => (e.project_id.clone(), "log", e.stage),
    };

    state.writer.submit(event, state.queue_capacity).await?;
    // Fire-and-forget on purpose. UDP has no acknowledgement, so waiting for
    // the commit here would stall the receive loop behind the disk and could
    // cause the kernel to drop subsequent datagrams.
    let _ = state.recent.send(RecentEvent {
        project_id,
        kind,
        stage,
        at: db::now_iso8601(),
    });
    tracing::debug!(%peer, kind, ?stage, "UDP packet accepted");
    Ok(())
}

/// Percent-decode a query parameter. Small helper so the crate does not need the
/// full `url` crate for one call site.
pub fn decode(input: &str) -> String {
    percent_encoding::percent_decode_str(input)
        .decode_utf8_lossy()
        .into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn error_mapping_distinguishes_caller_faults_from_ours() {
        // A validation failure is the caller's problem -> 400.
        let e = ApiError(HavaldarError::invalid("stage", "required"));
        let resp = e.into_response();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);

        // An unknown project is a 409, so the worker can distinguish it from a
        // transient fault and stop retrying.
        let e = ApiError(HavaldarError::Sqlite(rusqlite::Error::SqliteFailure(
            rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_CONSTRAINT),
            Some("FOREIGN KEY constraint failed".into()),
        )));
        let resp = e.into_response();
        assert_eq!(resp.status(), StatusCode::CONFLICT);
    }

    #[test]
    fn overload_returns_503_with_retry_after() {
        let e = ApiError(HavaldarError::Overloaded { capacity: 10 });
        let resp = e.into_response();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(
            resp.headers().get("retry-after").and_then(|v| v.to_str().ok()),
            Some("1")
        );
    }

    #[test]
    fn percent_decoding_round_trips() {
        assert_eq!(decode("plain"), "plain");
        assert_eq!(decode("a%20b"), "a b");
        assert_eq!(decode("%2Fpath"), "/path");
    }
}