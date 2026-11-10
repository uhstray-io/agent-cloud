//! Login sessions in the console's SQLite database (`<CONSOLE_DATA_DIR>/prefs.db`).
//!
//! A `tower_sessions` store on the preferences connection, so one file and one
//! lock serve both. A restart keeps every session that has not expired.
//!
//! Each row keeps the record's id (the cookie value), its JSON body and its expiry.
//! Session data is never written to a log or into an error: errors carry fixed text.

use std::future::Future;
use std::pin::Pin;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, OptionalExtension};
use tokio::sync::Mutex;
use tower_sessions::session::{Id, Record};
use tower_sessions::session_store::{self, SessionStore};

/// Expired rows are swept at most this often (seconds), and once at startup.
const SWEEP_EVERY_SECS: i64 = 3600;

const SCHEMA: &str = "CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    expiry_unix INTEGER NOT NULL
)";

/// Creates the table and drops expired rows. Runs once when the database opens.
pub fn init(conn: &Connection) -> rusqlite::Result<()> {
    conn.execute_batch(SCHEMA)?;
    sweep(conn, now_unix())
}

fn sweep(conn: &Connection, now: i64) -> rusqlite::Result<()> {
    conn.execute("DELETE FROM sessions WHERE expiry_unix <= ?1", params![now])?;
    Ok(())
}

fn now_unix() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| {
            i64::try_from(elapsed.as_secs()).unwrap_or(i64::MAX)
        })
}

fn backend(err: rusqlite::Error) -> session_store::Error {
    session_store::Error::Backend(format!("sessions: {err}"))
}

/// Session store over the shared preferences connection.
#[derive(Debug, Clone)]
pub struct SqliteStore {
    conn: Arc<Mutex<Connection>>,
    /// Unix seconds of the last sweep. Shared by every clone of the store.
    last_sweep: Arc<AtomicI64>,
}

impl SqliteStore {
    /// Wraps the preferences connection. `init` must have run on it first.
    pub fn new(conn: Arc<Mutex<Connection>>) -> Self {
        SqliteStore {
            conn,
            // `init` has just swept, so the next sweep is due an hour from now.
            last_sweep: Arc::new(AtomicI64::new(now_unix())),
        }
    }

    /// Sweeps expired rows when the last sweep is an hour old or more.
    fn sweep_if_due(&self, conn: &Connection) -> session_store::Result<()> {
        let now = now_unix();
        if now - self.last_sweep.load(Ordering::Relaxed) < SWEEP_EVERY_SECS {
            return Ok(());
        }
        sweep(conn, now).map_err(backend)?;
        self.last_sweep.store(now, Ordering::Relaxed);
        Ok(())
    }
}

fn row_exists(conn: &Connection, id: &Id) -> session_store::Result<bool> {
    let found: Option<i64> = conn
        .query_row(
            "SELECT 1 FROM sessions WHERE id = ?1",
            params![id.to_string()],
            |row| row.get(0),
        )
        .optional()
        .map_err(backend)?;
    Ok(found.is_some())
}

/// Upsert: inserts a new row, or replaces the body and expiry of an existing one.
fn write(conn: &Connection, record: &Record) -> session_store::Result<()> {
    let data = serde_json::to_string(record)
        .map_err(|_| session_store::Error::Encode("session record".to_string()))?;
    conn.execute(
        "INSERT INTO sessions (id, data, expiry_unix) VALUES (?1, ?2, ?3)
         ON CONFLICT(id) DO UPDATE SET
             data = excluded.data,
             expiry_unix = excluded.expiry_unix",
        params![
            record.id.to_string(),
            data,
            record.expiry_date.unix_timestamp()
        ],
    )
    .map_err(backend)?;
    Ok(())
}

/// Returns the live record. An expired row is deleted and reads as absent.
fn read(conn: &Connection, id: &Id, now: i64) -> session_store::Result<Option<Record>> {
    let key = id.to_string();
    let row: Option<(String, i64)> = conn
        .query_row(
            "SELECT data, expiry_unix FROM sessions WHERE id = ?1",
            params![key],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(backend)?;
    let Some((data, expiry_unix)) = row else {
        return Ok(None);
    };
    if expiry_unix <= now {
        conn.execute("DELETE FROM sessions WHERE id = ?1", params![key])
            .map_err(backend)?;
        return Ok(None);
    }
    let record = serde_json::from_str::<Record>(&data)
        .map_err(|_| session_store::Error::Decode("session record".to_string()))?;
    Ok(Some(record))
}

// The trait is `#[async_trait]` upstream. The crate is only a transitive dependency,
// so the boxed-future signatures are written out here. They match async_trait's expansion.
impl SessionStore for SqliteStore {
    fn create<'a, 'b, 'at>(
        &'a self,
        record: &'b mut Record,
    ) -> Pin<Box<dyn Future<Output = session_store::Result<()>> + Send + 'at>>
    where
        'a: 'at,
        'b: 'at,
        Self: 'at,
    {
        Box::pin(async move {
            let conn = self.conn.lock().await;
            // Never overwrite a live row: draw a fresh id until the slot is free.
            while row_exists(&conn, &record.id)? {
                record.id = Id::default();
            }
            write(&conn, record)
        })
    }

    fn save<'a, 'b, 'at>(
        &'a self,
        record: &'b Record,
    ) -> Pin<Box<dyn Future<Output = session_store::Result<()>> + Send + 'at>>
    where
        'a: 'at,
        'b: 'at,
        Self: 'at,
    {
        Box::pin(async move {
            let conn = self.conn.lock().await;
            write(&conn, record)?;
            self.sweep_if_due(&conn)
        })
    }

    fn load<'a, 'b, 'at>(
        &'a self,
        session_id: &'b Id,
    ) -> Pin<Box<dyn Future<Output = session_store::Result<Option<Record>>> + Send + 'at>>
    where
        'a: 'at,
        'b: 'at,
        Self: 'at,
    {
        Box::pin(async move {
            let conn = self.conn.lock().await;
            read(&conn, session_id, now_unix())
        })
    }

    fn delete<'a, 'b, 'at>(
        &'a self,
        session_id: &'b Id,
    ) -> Pin<Box<dyn Future<Output = session_store::Result<()>> + Send + 'at>>
    where
        'a: 'at,
        'b: 'at,
        Self: 'at,
    {
        Box::pin(async move {
            let conn = self.conn.lock().await;
            conn.execute(
                "DELETE FROM sessions WHERE id = ?1",
                params![session_id.to_string()],
            )
            .map_err(backend)?;
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use tower_sessions::cookie::time::OffsetDateTime;

    fn memory_store() -> SqliteStore {
        let conn = Connection::open_in_memory().expect("memory db");
        init(&conn).expect("schema");
        SqliteStore::new(Arc::new(Mutex::new(conn)))
    }

    fn record(expiry_unix: i64) -> Record {
        Record {
            id: Id::default(),
            data: HashMap::from([("user".to_string(), serde_json::json!({"sub": "user-1"}))]),
            expiry_date: OffsetDateTime::from_unix_timestamp(expiry_unix).expect("in range"),
        }
    }

    async fn stored_expiries(store: &SqliteStore) -> Vec<i64> {
        let conn = store.conn.lock().await;
        let mut stmt = conn
            .prepare("SELECT expiry_unix FROM sessions ORDER BY expiry_unix")
            .expect("prepare");
        stmt.query_map([], |row| row.get(0))
            .expect("query")
            .map(|row| row.expect("row"))
            .collect()
    }

    #[tokio::test]
    async fn save_then_load_returns_the_same_record() {
        let store = memory_store();
        let saved = record(now_unix() + 3600);
        store.save(&saved).await.expect("save");
        assert_eq!(store.load(&saved.id).await.expect("load"), Some(saved));
    }

    #[tokio::test]
    async fn save_replaces_the_body_of_the_same_id() {
        let store = memory_store();
        let mut session = record(now_unix() + 3600);
        store.save(&session).await.expect("first save");
        session
            .data
            .insert("user".into(), serde_json::json!({"sub": "user-2"}));
        store.save(&session).await.expect("second save");
        assert_eq!(store.load(&session.id).await.expect("load"), Some(session));
        assert_eq!(stored_expiries(&store).await.len(), 1);
    }

    #[tokio::test]
    async fn expired_record_is_not_loaded_and_its_row_is_removed() {
        let store = memory_store();
        let stale = record(now_unix() - 1);
        store.save(&stale).await.expect("save");
        assert_eq!(store.load(&stale.id).await.expect("load"), None);
        assert!(stored_expiries(&store).await.is_empty());
    }

    #[tokio::test]
    async fn delete_removes_the_record() {
        let store = memory_store();
        let session = record(now_unix() + 3600);
        store.save(&session).await.expect("save");
        store.delete(&session.id).await.expect("delete");
        assert_eq!(store.load(&session.id).await.expect("load"), None);
    }

    #[tokio::test]
    async fn create_never_overwrites_a_colliding_id() {
        let store = memory_store();
        let taken = record(now_unix() + 3600);
        store.save(&taken).await.expect("save");

        let mut fresh = record(now_unix() + 3600);
        fresh.id = taken.id;
        fresh
            .data
            .insert("user".into(), serde_json::json!({"sub": "user-2"}));
        store.create(&mut fresh).await.expect("create");

        assert_ne!(fresh.id, taken.id, "create must draw a new id");
        assert_eq!(
            store.load(&taken.id).await.expect("load taken"),
            Some(taken)
        );
        assert_eq!(
            store.load(&fresh.id).await.expect("load fresh"),
            Some(fresh)
        );
    }

    #[tokio::test]
    async fn startup_init_sweeps_expired_rows() {
        let store = memory_store();
        let stale = record(now_unix() - 1);
        store.save(&stale).await.expect("save");
        assert_eq!(
            stored_expiries(&store).await,
            vec![stale.expiry_date.unix_timestamp()]
        );

        init(&*store.conn.lock().await).expect("re-init");
        assert!(stored_expiries(&store).await.is_empty());
    }

    #[tokio::test]
    async fn save_sweeps_at_most_hourly() {
        let store = memory_store();
        let stale = record(now_unix() - 1);
        store.save(&stale).await.expect("save stale");
        store
            .save(&record(now_unix() + 3600))
            .await
            .expect("save live");
        assert_eq!(
            stored_expiries(&store).await.len(),
            2,
            "no sweep within the hour"
        );

        store
            .last_sweep
            .store(now_unix() - SWEEP_EVERY_SECS, Ordering::Relaxed);
        store
            .save(&record(now_unix() + 3600))
            .await
            .expect("save due");
        assert!(
            !stored_expiries(&store)
                .await
                .contains(&stale.expiry_date.unix_timestamp()),
            "the stale row goes once a sweep is due"
        );
    }
}
