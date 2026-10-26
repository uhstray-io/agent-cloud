//! Per-user preferences in SQLite (`<CONSOLE_DATA_DIR>/prefs.db`).
//!
//! One connection behind a tokio Mutex, shared with the session store (see
//! `sessions.rs`). That is fine at console scale: a few users, one row each, so
//! each query holds the lock for microseconds. A contended database would move
//! these calls into spawn_blocking.

use std::path::Path;
use std::sync::Arc;

use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;

use super::sessions::{self, SqliteStore};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Preferences {
    pub theme: String,
    pub timezone: String,
    pub default_scope: Option<String>,
}

impl Default for Preferences {
    fn default() -> Self {
        Preferences {
            theme: "system".to_string(),
            timezone: "UTC".to_string(),
            default_scope: None,
        }
    }
}

impl Preferences {
    /// Checks the contract's rules. The error is a fixed sentence, never the input.
    pub fn validate(self) -> Result<Preferences, &'static str> {
        if !matches!(self.theme.as_str(), "system" | "light" | "dark") {
            return Err("theme must be one of system, light, dark");
        }
        let tz_ok = (1..=64).contains(&self.timezone.chars().count())
            && self
                .timezone
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '+' | '-' | '/'));
        if !tz_ok {
            return Err("timezone must be 1-64 characters from A-Z a-z 0-9 _ + - /");
        }
        if let Some(scope) = &self.default_scope {
            if !(1..=128).contains(&scope.chars().count()) {
                return Err("default_scope must be null or 1-128 characters");
            }
        }
        Ok(self)
    }
}

const SCHEMA: &str = "CREATE TABLE IF NOT EXISTS preferences (
    sub TEXT PRIMARY KEY,
    theme TEXT NOT NULL DEFAULT 'system',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    default_scope TEXT,
    updated_at TEXT NOT NULL
)";

pub struct Prefs {
    conn: Arc<Mutex<Connection>>,
}

impl Prefs {
    pub fn open(path: &Path) -> rusqlite::Result<Self> {
        Self::from_connection(Connection::open(path)?)
    }

    #[cfg(test)]
    pub fn in_memory() -> rusqlite::Result<Self> {
        Self::from_connection(Connection::open_in_memory()?)
    }

    fn from_connection(conn: Connection) -> rusqlite::Result<Self> {
        conn.execute_batch(SCHEMA)?;
        sessions::init(&conn)?;
        Ok(Prefs {
            conn: Arc::new(Mutex::new(conn)),
        })
    }

    /// The session store over this same connection, so sessions live in `prefs.db`.
    pub fn session_store(&self) -> SqliteStore {
        SqliteStore::new(self.conn.clone())
    }

    /// Stored preferences, or the defaults when the user has none yet.
    pub async fn get(&self, sub: &str) -> rusqlite::Result<Preferences> {
        let conn = self.conn.lock().await;
        let stored = conn
            .query_row(
                "SELECT theme, timezone, default_scope FROM preferences WHERE sub = ?1",
                [sub],
                |row| {
                    Ok(Preferences {
                        theme: row.get(0)?,
                        timezone: row.get(1)?,
                        default_scope: row.get(2)?,
                    })
                },
            )
            .optional()?;
        Ok(stored.unwrap_or_default())
    }

    pub async fn put(&self, sub: &str, prefs: &Preferences) -> rusqlite::Result<()> {
        let conn = self.conn.lock().await;
        conn.execute(
            "INSERT INTO preferences (sub, theme, timezone, default_scope, updated_at)
             VALUES (?1, ?2, ?3, ?4, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
             ON CONFLICT(sub) DO UPDATE SET
                 theme = excluded.theme,
                 timezone = excluded.timezone,
                 default_scope = excluded.default_scope,
                 updated_at = excluded.updated_at",
            params![sub, prefs.theme, prefs.timezone, prefs.default_scope],
        )?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn prefs(theme: &str, timezone: &str, scope: Option<&str>) -> Preferences {
        Preferences {
            theme: theme.into(),
            timezone: timezone.into(),
            default_scope: scope.map(str::to_string),
        }
    }

    #[test]
    fn accepts_contract_values() {
        assert!(prefs("dark", "America/Chicago", None).validate().is_ok());
        assert!(prefs("system", "UTC", Some("acme/ops")).validate().is_ok());
        assert!(prefs("light", "Etc/GMT+5", None).validate().is_ok());
    }

    #[test]
    fn rejects_unknown_theme() {
        assert_eq!(
            prefs("neon", "UTC", None).validate(),
            Err("theme must be one of system, light, dark")
        );
    }

    #[test]
    fn rejects_bad_timezone() {
        assert!(prefs("dark", "", None).validate().is_err());
        assert!(prefs("dark", "Europe/London west", None)
            .validate()
            .is_err());
        assert!(prefs("dark", &"a".repeat(65), None).validate().is_err());
        assert!(prefs("dark", &"a".repeat(64), None).validate().is_ok());
    }

    #[test]
    fn rejects_bad_default_scope() {
        assert!(prefs("dark", "UTC", Some("")).validate().is_err());
        assert!(prefs("dark", "UTC", Some(&"s".repeat(129)))
            .validate()
            .is_err());
        assert!(prefs("dark", "UTC", Some(&"s".repeat(128)))
            .validate()
            .is_ok());
    }

    #[test]
    fn unknown_field_is_refused_by_serde() {
        let parsed: Result<Preferences, _> = serde_json::from_str(r#"{"theme":"dark","extra":1}"#);
        assert!(parsed.is_err());
        let defaulted: Preferences = serde_json::from_str("{}").expect("all fields default");
        assert_eq!(defaulted, Preferences::default());
    }

    #[tokio::test]
    async fn sqlite_roundtrip_in_memory() {
        let db = Prefs::in_memory().expect("memory db with schema");
        assert_eq!(
            db.get("user-1").await.expect("read"),
            Preferences::default()
        );

        let chosen = prefs("dark", "Europe/Berlin", Some("acme/ops"));
        db.put("user-1", &chosen).await.expect("write");
        assert_eq!(db.get("user-1").await.expect("read back"), chosen);

        let replaced = prefs("light", "UTC", None);
        db.put("user-1", &replaced).await.expect("upsert");
        assert_eq!(db.get("user-1").await.expect("read again"), replaced);

        assert_eq!(
            db.get("user-2").await.expect("other user"),
            Preferences::default()
        );
    }
}
