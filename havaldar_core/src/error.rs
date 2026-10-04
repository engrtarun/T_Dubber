//! Crate-wide error handling.
//!
//! One rule governs this file: nothing in a request path may `unwrap()`, `expect()`
//! or `panic!`. Every fallible operation returns `Result`, and every error that
//! crosses the HTTP boundary is converted into a status code by
//! [`ApiError::into_response`].
//!
//! `unsafe_code = "forbid"` is set in Cargo.toml, so this crate cannot contain
//! an unsafe block at all. That is the "memory-safe" claim enforced by the
//! compiler rather than asserted in a README.

use std::fmt;

/// Result alias used throughout the crate.
pub type Result<T> = std::result::Result<T, HavaldarError>;

/// Every failure mode the daemon can produce.
///
/// Variants carry enough context to debug from a log line alone, because a
/// telemetry daemon that only says "database error" is worse than useless at
/// 3am when a Kaggle run is on the line.
#[derive(Debug)]
pub enum HavaldarError {
    /// SQLite failed. `rusqlite`'s own error is retained, not stringified, so
    /// the `code` field survives for programmatic handling.
    Sqlite(rusqlite::Error),

    /// A packet failed schema or semantic validation. `field` names the
    /// offending part so a 400 response can tell the caller exactly what to fix.
    Validation {
        field: &'static str,
        detail: String,
    },

    /// The writer thread is gone. Almost always means the DB thread panicked,
    /// so the message says so rather than reporting a generic channel error.
    WriterGone,

    /// The incoming queue is full and we refused to buffer more.
    Overloaded { capacity: usize },

    /// A string could not be parsed (CLI args, headers).
    Config(String),

    /// Serialisation / deserialisation failure.
    Serde(serde_json::Error),

    /// Anything IO that is not the database (listening socket, file, shutdown).
    Io(std::io::Error),
}

impl fmt::Display for HavaldarError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Sqlite(e) => write!(f, "sqlite error: {e}"),
            Self::Validation { field, detail } => {
                write!(f, "invalid telemetry packet: field `{field}` {detail}")
            }
            Self::WriterGone => write!(
                f,
                "the database writer thread is unavailable (it panicked, or the \
                 database could not be opened at startup)"
            ),
            Self::Overloaded { capacity } => write!(
                f,
                "ingestion queue is full ({capacity} packets buffered); \
                 refusing more work rather than growing without bound"
            ),
            Self::Config(m) => write!(f, "configuration error: {m}"),
            Self::Serde(e) => write!(f, "json error: {e}"),
            Self::Io(e) => write!(f, "io error: {e}"),
        }
    }
}

impl std::error::Error for HavaldarError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Sqlite(e) => Some(e),
            Self::Serde(e) => Some(e),
            Self::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<rusqlite::Error> for HavaldarError {
    fn from(e: rusqlite::Error) -> Self {
        Self::Sqlite(e)
    }
}

impl From<serde_json::Error> for HavaldarError {
    fn from(e: serde_json::Error) -> Self {
        Self::Serde(e)
    }
}

impl From<std::io::Error> for HavaldarError {
    fn from(e: std::io::Error) -> Self {
        Self::Io(e)
    }
}

impl HavaldarError {
    /// Convenience constructor for validation failures.
    pub fn invalid(field: &'static str, detail: impl Into<String>) -> Self {
        Self::Validation {
            field,
            detail: detail.into(),
        }
    }

    /// True when the error is a constraint violation rather than a real fault.
    ///
    /// The important one is the foreign key on `pipeline_stages.project_id`.
    /// `db.py` sets `PRAGMA foreign_keys=ON`, so a stage packet naming a
    /// project that does not exist is rejected by SQLite. That is a client bug
    /// (unknown project) and must be a 409, not a 500 that looks like the
    /// daemon is broken.
    pub fn is_constraint_violation(&self) -> bool {
        matches!(
            self,
            Self::Sqlite(rusqlite::Error::SqliteFailure(e, _))
                if e.code == rusqlite::ErrorCode::ConstraintViolation
        )
    }

    /// True when the error is a lock/timeout, i.e. contention rather than fault.
    ///
    /// `db.py` opens the same database with a 15 s busy timeout, so a writer
    /// clash is expected under load and is not an error condition to alarm on.
    pub fn is_busy(&self) -> bool {
        match self {
            Self::Sqlite(rusqlite::Error::SqliteFailure(e, _)) => {
                e.code == rusqlite::ErrorCode::DatabaseBusy
                    || e.code == rusqlite::ErrorCode::DatabaseLocked
            }
            _ => false,
        }
    }
}