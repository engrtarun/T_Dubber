//! Telemetry packet schema and validation.
//!
//! # Compatibility with the existing writer
//!
//! `db.py`'s `record_stage()` writes `status` as one of
//! `running | success | failed | skipped`, and `dashboard_server.py` maps those
//! to the UI's `running/done/error/queued`. The example packet in the spec used
//! `"processing"`, which is **not** one of those values -- written verbatim it
//! would produce a row the dashboard cannot render.
//!
//! [`StageStatus::parse`] therefore accepts the Kaggle-ish spellings people
//! actually send (`processing`, `in_progress`, `running`, `ok`, `done`,
//! `complete`, `error`, `failed`, `skipped`, `pending`) and normalises them to
//! the four values `db.py` writes. Unknown values are rejected with a clear
//! error rather than stored, because a silent typo in a status column is
//! invisible until someone is staring at a stuck pipeline at 3am.

use serde::{Deserialize, Serialize};

use crate::error::{HavaldarError, Result};

/// Canonical status vocabulary, kept byte-identical to what `db.py` writes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StageStatus {
    Running,
    Success,
    Failed,
    Skipped,
}

impl StageStatus {
    /// Normalise a wire string to a canonical status.
    ///
    /// Returns `None` for unknown input so the caller can decide whether to
    /// reject the packet or fall back.
    pub fn parse(raw: &str) -> Option<Self> {
        match raw.trim().to_ascii_lowercase().as_str() {
            // In-flight spellings. "processing" is what the Kaggle worker
            // naturally says, and it means the same thing as db.py's "running".
            "running" | "processing" | "in_progress" | "in-progress" | "start"
            | "started" | "working" => Some(Self::Running),

            // Terminal-success spellings.
            "success" | "ok" | "done" | "complete" | "completed" | "finished"
            | "passed" => Some(Self::Success),

            // Terminal-failure spellings.
            "failed" | "error" | "failure" | "crashed" | "timeout" | "fault" => {
                Some(Self::Failed)
            }

            "skipped" | "bypassed" => Some(Self::Skipped),

            _ => None,
        }
    }

    /// The literal written into the `status` column.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Running => "running",
            Self::Success => "success",
            Self::Failed => "failed",
            Self::Skipped => "skipped",
        }
    }

    /// Whether this status should stamp `finished_at`.
    ///
    /// Mirrors `db.py::record_stage`, which only sets `finished_at` for
    /// `success | failed | skipped`. Getting this wrong would overwrite a real
    /// finish time with NULL on every "running" packet that arrives late.
    pub fn stamps_finished_at(self) -> bool {
        !matches!(self, Self::Running)
    }

    /// Whether this status should stamp `started_at`.
    ///
    /// Only `running` stamps `started_at`. Note `db.py`'s `ON CONFLICT` clause
    /// does **not** update `started_at`, so the first "running" packet for a
    /// stage is the one that matters -- which is why `seen_running` is tracked
    /// in [`StageEvent::normalize`].
    pub fn stamps_started_at(self) -> bool {
        matches!(self, Self::Running)
    }
}

/// Hard bounds applied before anything reaches SQLite.
///
/// These are not paranoia. A Kaggle worker can and does retry a failing POST,
/// and an unbounded `message` would let one buggy client write megabytes per
/// packet into a database that Python also writes. `db.py` truncates to 500 /
/// 2000 chars; we match it so both writers produce comparable rows.
pub const MAX_MESSAGE_LEN: usize = 500;
pub const MAX_ERROR_LEN: usize = 2_000;
pub const MAX_PROJECT_ID_LEN: usize = 255;
pub const MAX_STAGE_NAME_LEN: usize = 128;

/// One telemetry packet.
///
/// `project_id` + `stage` + `status` identify the row. `stage_name` is optional
/// and, when supplied, is used to derive a canonical name from
/// `STAGE_NAMES` so the dashboard's rail stays consistent.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct TelemetryPacket {
    /// `projects.id`. Required for stage packets.
    pub project_id: Option<String>,

    /// Zero-based stage index, matching the `[STAGE:n]` markers in `pipeline.py`
    /// and the rail in `dashboard/index.html`.
    pub stage: Option<i64>,

    /// Free-form stage label; canonicalised when it matches a known stage.
    pub stage_name: Option<String>,

    /// Raw status; normalised by [`StageStatus::parse`].
    pub status: Option<String>,

    pub message: Option<String>,

    pub error: Option<String>,

    /// Explicit duration override. When absent, computed from arrival times.
    pub duration_sec: Option<f64>,

    /// For log-only packets: severity. Free text, stored as-is.
    pub level: Option<String>,

    /// Optional client-supplied timestamp (RFC 3339). Used for ordering only;
    /// never for `started_at`/`finished_at`, which the daemon stamps itself so
    /// two writers cannot disagree about the clock.
    pub ts: Option<String>,
}

/// A validated packet, ready for insertion.
///
/// Constructing this type is the only way to reach the writer, which is what
/// guarantees every packet has been length-checked and status-normalised.
#[derive(Debug, Clone)]
pub enum TelemetryEvent {
    Stage(StageEvent),
    Log(LogEvent),
}

impl TelemetryEvent {
    /// Project this event belongs to, for logging.
    pub fn project_id(&self) -> &str {
        match self {
            Self::Stage(e) => &e.project_id,
            Self::Log(e) => &e.project_id,
        }
    }
}

/// A validated stage transition.
#[derive(Debug, Clone)]
pub struct StageEvent {
    pub project_id: String,
    pub stage: i64,
    pub stage_name: String,
    pub status: StageStatus,
    pub message: Option<String>,
    pub error: Option<String>,
    pub duration_sec: Option<f64>,
    /// True when this is the first `running` we have seen for this stage, i.e.
    /// the row does not exist yet and `started_at` will be written by the
    /// INSERT branch rather than lost to the ON CONFLICT UPDATE branch.
    pub first_running: bool,
}

/// A validated log line.
#[derive(Debug, Clone)]
pub struct LogEvent {
    pub project_id: String,
    pub stage: Option<i64>,
    pub level: String,
    pub message: String,
}

/// Canonical stage names, identical to `pipeline.py`'s `STAGE_NAMES`.
///
/// Kept in sync by construction: the list below is the same nine entries, in
/// the same order, with the same strings.
pub const STAGE_NAMES: [&str; 9] = [
    "Resolve", "Compress", "Bundle", "Dataset", "Kernel", "GPU Worker", "Download", "Verify",
    "Transport",
];

/// Derive a canonical stage name from an index.
///
/// Unknown indices get a generic `Stage N` rather than being rejected, because
/// `pipeline.py` can legitimately grow a stage and a 400 would break the run.
fn canonical_stage_name(stage: i64, supplied: Option<&str>) -> String {
    let from_index = usize::try_from(stage)
        .ok()
        .and_then(|i| STAGE_NAMES.get(i).copied());

    // Prefer the index: it is the single source of truth, and the label is only
    // cosmetic. A mismatched label from a stale client must not rewrite it.
    match from_index {
        Some(name) => name.to_string(),
        None => supplied
            .map(|s| s.to_string())
            .unwrap_or_else(|| format!("Stage {stage}")),
    }
}

/// Truncate to `max` bytes on a char boundary, appending an ellipsis marker.
///
/// Byte slicing a UTF-8 string at an arbitrary index panics. Since messages
/// come from a network socket, that is a remotely triggerable panic unless we
/// are careful here.
pub fn truncate_chars(s: &str, max: usize) -> String {
    if s.len() <= max {
        return s.to_string();
    }
    // Walk back to a char boundary within the budget.
    let mut end = max.saturating_sub(3);
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}...", &s[..end])
}

impl StageEvent {
    /// Build a validated stage event, or explain precisely what is wrong.
    ///
    /// `seen_running` is the caller's per-(project, stage) view of whether a
    /// `running` packet has already been persisted. It is passed in rather than
    /// kept here because the writer owns that state.
    pub fn normalize(packet: &TelemetryPacket, seen_running: bool) -> Result<Self> {
        let project_id = packet
            .project_id
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| HavaldarError::invalid("project_id", "is required and must not be empty"))?;
        if project_id.len() > MAX_PROJECT_ID_LEN {
            return Err(HavaldarError::invalid(
                "project_id",
                format!("exceeds {MAX_PROJECT_ID_LEN} bytes"),
            ));
        }

        let stage = packet
            .stage
            .ok_or_else(|| HavaldarError::invalid("stage", "is required for a stage packet"))?;
        if !(-1..=64).contains(&stage) {
            return Err(HavaldarError::invalid(
                "stage",
                format!("{stage} is out of the accepted range -1..=64"),
            ));
        }

        let raw_status = packet
            .status
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| HavaldarError::invalid("status", "is required for a stage packet"))?;
        let status = StageStatus::parse(raw_status).ok_or_else(|| {
            HavaldarError::invalid(
                "status",
                format!(
                    "{raw_status:?} is not a recognised status; expected one of \
                     running/processing/success/done/failed/error/skipped"
                ),
            )
        })?;

        let supplied_name = packet.stage_name.as_deref().map(str::trim);
        if let Some(name) = supplied_name {
            if name.len() > MAX_STAGE_NAME_LEN {
                return Err(HavaldarError::invalid(
                    "stage_name",
                    format!("exceeds {MAX_STAGE_NAME_LEN} bytes"),
                ));
            }
        }

        // duration_sec must be finite and non-negative. `f64::from(NaN)` from a
        // JSON `NaN` literal would otherwise be stored as a non-finite REAL,
        // which SQLite accepts and every consumer then has to defend against.
        let duration_sec = match packet.duration_sec {
            Some(d) if d.is_finite() && d >= 0.0 => Some(d),
            Some(_) => {
                return Err(HavaldarError::invalid(
                    "duration_sec",
                    "must be a finite, non-negative number",
                ))
            }
            None => None,
        };

        Ok(Self {
            project_id: project_id.to_string(),
            stage,
            stage_name: canonical_stage_name(stage, supplied_name),
            status,
            message: packet.message.as_deref().map(|m| truncate_chars(m.trim(), MAX_MESSAGE_LEN)),
            error: packet.error.as_deref().map(|e| truncate_chars(e.trim(), MAX_ERROR_LEN)),
            duration_sec,
            first_running: status.stamps_started_at() && !seen_running,
        })
    }
}

impl LogEvent {
    pub fn normalize(packet: &TelemetryPacket) -> Result<Self> {
        let project_id = packet
            .project_id
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| HavaldarError::invalid("project_id", "is required and must not be empty"))?;

        let message = packet
            .message
            .as_deref()
            .map(str::trim)
            .filter(|m| !m.is_empty())
            .ok_or_else(|| HavaldarError::invalid("message", "is required for a log packet"))?;

        // Level is free text on purpose -- `db.py` has no vocabulary for it and
        // inventing one here would reject valid callers. Bounded and lowercased.
        let level = packet
            .level
            .as_deref()
            .map(str::trim)
            .filter(|l| !l.is_empty())
            .unwrap_or("info")
            .to_ascii_lowercase();
        if level.len() > 32 {
            return Err(HavaldarError::invalid("level", "exceeds 32 bytes"));
        }

        Ok(Self {
            project_id: project_id.to_string(),
            stage: packet.stage,
            level,
            message: truncate_chars(message, MAX_ERROR_LEN),
        })
    }
}

impl TelemetryPacket {
    /// Decide what kind of event this packet is and validate it.
    ///
    /// A packet with a `status` is a stage transition; otherwise it is a log
    /// line. This mirrors how `pipeline.py` uses the two tables: `track_stage`
    /// for transitions, and plain strings for the event stream.
    pub fn into_event(self, seen_running: bool) -> Result<TelemetryEvent> {
        let is_stage = self.status.as_deref().map(|s| !s.trim().is_empty()).unwrap_or(false)
            || self.stage.is_some();

        if is_stage {
            Ok(TelemetryEvent::Stage(StageEvent::normalize(&self, seen_running)?))
        } else {
            Ok(TelemetryEvent::Log(LogEvent::normalize(&self)?))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_aliases_normalise_to_db_py_vocabulary() {
        // The spec's example used "processing", which db.py never writes.
        assert_eq!(StageStatus::parse("processing"), Some(StageStatus::Running));
        assert_eq!(StageStatus::parse("PROCESSING"), Some(StageStatus::Running));
        assert_eq!(StageStatus::parse("done"), Some(StageStatus::Success));
        assert_eq!(StageStatus::parse("ok"), Some(StageStatus::Success));
        assert_eq!(StageStatus::parse("error"), Some(StageStatus::Failed));
        assert_eq!(StageStatus::parse("timeout"), Some(StageStatus::Failed));
        assert_eq!(StageStatus::parse("skipped"), Some(StageStatus::Skipped));
        assert_eq!(StageStatus::parse("banana"), None);
    }

    #[test]
    fn as_str_matches_db_py_literals() {
        assert_eq!(StageStatus::Running.as_str(), "running");
        assert_eq!(StageStatus::Success.as_str(), "success");
        assert_eq!(StageStatus::Failed.as_str(), "failed");
        assert_eq!(StageStatus::Skipped.as_str(), "skipped");
    }

    #[test]
    fn finished_at_only_stamped_on_terminal_states() {
        assert!(!StageStatus::Running.stamps_finished_at());
        assert!(StageStatus::Success.stamps_finished_at());
        assert!(StageStatus::Failed.stamps_finished_at());
        assert!(StageStatus::Skipped.stamps_finished_at());
        assert!(StageStatus::Running.stamps_started_at());
        assert!(!StageStatus::Success.stamps_started_at());
    }

    fn stage_packet(status: &str) -> TelemetryPacket {
        TelemetryPacket {
            project_id: Some("proj-1".into()),
            stage: Some(4),
            stage_name: None,
            status: Some(status.into()),
            message: None,
            error: None,
            duration_sec: None,
            level: None,
            ts: None,
        }
    }

    #[test]
    fn canonical_names_match_pipeline_py() {
        let p = stage_packet("running");
        let e = StageEvent::normalize(&p, false).expect("valid");
        assert_eq!(e.stage_name, "Kernel");
        assert_eq!(e.stage, 4);
        assert!(e.first_running, "first running must be flagged");
    }

    #[test]
    fn second_running_is_not_first_running() {
        let p = stage_packet("running");
        let e = StageEvent::normalize(&p, true).expect("valid");
        assert!(!e.first_running);
    }

    #[test]
    fn supplied_label_cannot_override_the_index() {
        let mut p = stage_packet("running");
        p.stage_name = Some("Totally Wrong".into());
        let e = StageEvent::normalize(&p, false).expect("valid");
        assert_eq!(e.stage_name, "Kernel");
    }

    #[test]
    fn out_of_range_stage_gets_generic_name() {
        let mut p = stage_packet("running");
        p.stage = Some(99);
        let e = StageEvent::normalize(&p, false).expect("valid");
        assert_eq!(e.stage_name, "Stage 99");
    }

    #[test]
    fn missing_required_fields_are_rejected() {
        let mut p = stage_packet("running");
        p.project_id = None;
        assert!(StageEvent::normalize(&p, false).is_err());

        let mut p = stage_packet("running");
        p.project_id = Some("   ".into());
        assert!(StageEvent::normalize(&p, false).is_err(), "blank id must fail");

        let mut p = stage_packet("running");
        p.stage = None;
        assert!(StageEvent::normalize(&p, false).is_err());

        let mut p = stage_packet("running");
        p.status = Some("nonsense".into());
        assert!(StageEvent::normalize(&p, false).is_err());
    }

    #[test]
    fn nan_duration_is_rejected_not_stored() {
        let mut p = stage_packet("success");
        p.duration_sec = Some(f64::NAN);
        assert!(StageEvent::normalize(&p, false).is_err());

        let mut p = stage_packet("success");
        p.duration_sec = Some(-1.0);
        assert!(StageEvent::normalize(&p, false).is_err());
    }

    #[test]
    fn multibyte_truncation_never_splits_a_char() {
        // A panic here would be remotely triggerable: messages arrive by socket.
        let s = "é".repeat(1000);
        let t = truncate_chars(&s, 500);
        assert!(t.len() <= 500);
        assert!(t.ends_with("..."));
        assert!(std::str::from_utf8(t.as_bytes()).is_ok());
        assert!(t.chars().all(|c| c == 'é' || c == '.'));
    }

    #[test]
    fn short_strings_are_untouched() {
        assert_eq!(truncate_chars("hello", 500), "hello");
        assert_eq!(truncate_chars("", 10), "");
    }

    #[test]
    fn log_packets_need_a_message() {
        let p = TelemetryPacket {
            project_id: Some("proj-1".into()),
            stage: None,
            stage_name: None,
            status: None,
            message: None,
            error: None,
            duration_sec: None,
            level: None,
            ts: None,
        };
        assert!(LogEvent::normalize(&p).is_err());
    }

    #[test]
    fn log_level_defaults_and_is_bounded() {
        let p = TelemetryPacket {
            project_id: Some("proj-1".into()),
            stage: None,
            stage_name: None,
            status: None,
            message: Some("hello".into()),
            error: None,
            duration_sec: None,
            level: None,
            ts: None,
        };
        let e = LogEvent::normalize(&p).expect("valid");
        assert_eq!(e.level, "info");

        let p2 = TelemetryPacket {
            level: Some("x".repeat(100)),
            ..p
        };
        assert!(LogEvent::normalize(&p2).is_err());
    }

    #[test]
    fn a_status_makes_it_a_stage_packet() {
        let p = stage_packet("running");
        assert!(matches!(p.into_event(false), Ok(TelemetryEvent::Stage(_))));
    }

    #[test]
    fn no_status_and_no_stage_makes_it_a_log_packet() {
        let p = TelemetryPacket {
            project_id: Some("proj-1".into()),
            stage: None,
            stage_name: None,
            status: None,
            message: Some("a line".into()),
            error: None,
            duration_sec: None,
            level: Some("warn".into()),
            ts: None,
        };
        assert!(matches!(p.into_event(false), Ok(TelemetryEvent::Log(_))));
    }
}