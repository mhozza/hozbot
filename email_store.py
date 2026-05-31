import sqlite3
import os
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "storage", "hozbot.db")


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS emails (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                uid         TEXT    NOT NULL,
                message_id  TEXT,
                sender      TEXT    NOT NULL,
                subject     TEXT    NOT NULL,
                body        TEXT    NOT NULL DEFAULT '',
                received_at TEXT,
                fetched_at  TEXT    NOT NULL,
                is_read     INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_emails_uid
                ON emails(uid);
            CREATE INDEX IF NOT EXISTS idx_emails_sender
                ON emails(sender);
            CREATE INDEX IF NOT EXISTS idx_emails_subject
                ON emails(subject);
            CREATE INDEX IF NOT EXISTS idx_emails_fetched_at
                ON emails(fetched_at);

            CREATE TABLE IF NOT EXISTS attachments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id    INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
                filename    TEXT    NOT NULL,
                mime_type   TEXT    NOT NULL DEFAULT '',
                size_bytes  INTEGER NOT NULL DEFAULT 0,
                local_path  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_attachments_email_id
                ON attachments(email_id);
        """)
    logger.info("Database initialised at %s", DB_PATH)


def store_email(email_data: dict[str, Any]) -> int | None:
    fetched_at = datetime.now(timezone.utc).isoformat()
    received_at = email_data.get("date") or fetched_at
    uid = email_data["uid"]

    with _get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM emails WHERE uid = ?", (uid,)
        ).fetchone()
        if existing:
            email_id = existing["id"]
        else:
            try:
                cur = conn.execute(
                    """INSERT INTO emails
                       (uid, message_id, sender, subject, body, received_at, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        uid,
                        email_data.get("message_id"),
                        email_data["sender"],
                        email_data["subject"],
                        email_data["body_snippet"],
                        received_at,
                        fetched_at,
                    ),
                )
                email_id = cur.lastrowid
            except Exception as e:
                logger.error("Failed to store email UID %s: %s", uid, e)
                return None

        attachments = email_data.get("attachments") or []
        if attachments:
            _store_attachments(conn, email_id, attachments)

        return email_id


def _store_attachments(
    conn: sqlite3.Connection, email_id: int, attachments: list[dict[str, Any]]
) -> None:
    existing = {
        r["filename"]
        for r in conn.execute(
            "SELECT filename FROM attachments WHERE email_id = ?", (email_id,)
        ).fetchall()
    }
    rows = [
        (email_id, a["filename"], a.get("mime_type", ""), a.get("size_bytes", 0))
        for a in attachments
        if a["filename"] not in existing
    ]
    if rows:
        conn.executemany(
            "INSERT INTO attachments (email_id, filename, mime_type, size_bytes) VALUES (?, ?, ?, ?)",
            rows,
        )


def search_emails(query: str, limit: int = 20) -> list[dict[str, Any]]:
    pattern = f"%{query}%"
    with _get_connection() as conn:
        rows = conn.execute(
            """SELECT id, uid, sender, subject, substr(body, 1, 200) AS body_snippet,
                      received_at, fetched_at, is_read
               FROM emails
               WHERE sender LIKE ? OR subject LIKE ? OR body LIKE ?
               ORDER BY fetched_at DESC
               LIMIT ?""",
            (pattern, pattern, pattern, limit),
        ).fetchall()
        results = []
        for row in rows:
            r = dict(row)
            r["attachments"] = _get_attachments_for_email(conn, r["id"])
            results.append(r)
        return results


def get_recent_emails(limit: int = 10) -> list[dict[str, Any]]:
    with _get_connection() as conn:
        rows = conn.execute(
            """SELECT id, uid, sender, subject, substr(body, 1, 200) AS body_snippet,
                      received_at, fetched_at, is_read
               FROM emails
               ORDER BY fetched_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        results = []
        for row in rows:
            r = dict(row)
            r["attachments"] = _get_attachments_for_email(conn, r["id"])
            results.append(r)
        return results


def get_emails_since(timestamp_iso: str) -> list[dict[str, Any]]:
    """Return emails where fetched_at > timestamp_iso, ordered newest first."""
    with _get_connection() as conn:
        rows = conn.execute(
            """SELECT id, uid, sender, subject
               FROM emails
               WHERE fetched_at > ?
               ORDER BY fetched_at DESC""",
            (timestamp_iso,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_email(email_id: int) -> dict[str, Any] | None:
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM emails WHERE id = ?", (email_id,)
        ).fetchone()
        if row is None:
            return None
        r = dict(row)
        r["attachments"] = _get_attachments_for_email(conn, email_id)
        return r


def get_email_by_uid(uid: str) -> dict[str, Any] | None:
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM emails WHERE uid = ?", (uid,)
        ).fetchone()
        if row is None:
            return None
        r = dict(row)
        r["attachments"] = _get_attachments_for_email(conn, r["id"])
        return r


def get_downloaded_path(uid: str, filename: str) -> str | None:
    with _get_connection() as conn:
        row = conn.execute(
            """SELECT a.local_path, a.email_id FROM attachments a
               JOIN emails e ON a.email_id = e.id
               WHERE e.uid = ? AND a.filename = ?""",
            (uid, filename),
        ).fetchone()
        if row and row["local_path"]:
            if os.path.exists(row["local_path"]):
                return row["local_path"]
            conn.execute(
                "UPDATE attachments SET local_path = NULL WHERE email_id = ? AND filename = ?",
                (row["email_id"], filename),
            )
        return None


def update_attachment_local_path(
    email_id: int, filename: str, local_path: str
) -> None:
    with _get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM attachments WHERE email_id = ? AND filename = ?",
            (email_id, filename),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE attachments SET local_path = ? WHERE id = ?",
                (local_path, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO attachments (email_id, filename, mime_type, size_bytes, local_path) VALUES (?, ?, '', 0, ?)",
                (email_id, filename, local_path),
            )


def _get_attachments_for_email(
    conn: sqlite3.Connection, email_id: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT filename, mime_type, size_bytes, local_path FROM attachments WHERE email_id = ?",
        (email_id,),
    ).fetchall()
    return [dict(r) for r in rows]
