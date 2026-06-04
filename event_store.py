import os
import logging
from datetime import datetime, timezone
from typing import Any

import email_store

logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "storage", "hozbot.db")


def _get_connection():
    return email_store._get_connection()


def add_event(
    title: str,
    start_iso: str,
    end_iso: str | None = None,
    source_email_id: int | None = None,
) -> int:
    created_at = datetime.now(timezone.utc).isoformat()
    with _get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO email_events
               (title, start_iso, end_iso, source_email_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (title, start_iso, end_iso, source_email_id, created_at),
        )
        return cur.lastrowid


def get_event(event_id: int) -> dict[str, Any] | None:
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM email_events WHERE id = ?", (event_id,)
        ).fetchone()
        return dict(row) if row else None


def get_future_events(days: int = 90) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM email_events WHERE start_iso >= ? ORDER BY start_iso",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_events(since_iso: str) -> list[dict[str, Any]]:
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM email_events WHERE created_at > ? ORDER BY start_iso",
            (since_iso,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_synced(event_id: int, google_event_id: str) -> None:
    with _get_connection() as conn:
        conn.execute(
            "UPDATE email_events SET synced_to_gcal = 1, google_event_id = ? WHERE id = ?",
            (google_event_id, event_id),
        )


def delete_event(event_id: int) -> None:
    with _get_connection() as conn:
        conn.execute("DELETE FROM email_events WHERE id = ?", (event_id,))


def get_event_by_gcal_id(google_event_id: str) -> dict[str, Any] | None:
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM email_events WHERE google_event_id = ?",
            (google_event_id,),
        ).fetchone()
        return dict(row) if row else None
