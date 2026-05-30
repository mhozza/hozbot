import json
import os
import uuid
import fcntl
from typing import List, Dict, Any, Callable

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PROFILE_PATH = os.path.join(BASE_DIR, "storage", "family_profile.json")
CALENDAR_PATH = os.path.join(BASE_DIR, "storage", "calendar_db.json")


def _default_for(path: str) -> Any:
    """Return a sensible default for a known storage path."""
    return [] if path.endswith("_db.json") else {}


def _read_json(path: str) -> Any:
    """Read a JSON file under a shared (non‑exclusive) lock.

    Returns the default value for missing, empty, or corrupt files.
    """
    try:
        with open(path, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            raw = f.read().strip()
            if not raw:
                return _default_for(path)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return _default_for(path)
    except FileNotFoundError:
        return _default_for(path)


def _update_json(path: str, fn: Callable[[Any], Any]) -> Any:
    """Atomically read‑modify‑write a JSON file under an exclusive lock.

    Opens (or creates) *path*, acquires ``LOCK_EX``, reads and deserialises
    the content (handling empty / corrupt files), applies *fn* to the data,
    and writes the result via a temp file + atomic ``os.rename``.

    Returns the value returned by *fn*.
    """
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read().strip()
        if not raw:
            data = _default_for(path)
        else:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = _default_for(path)
        new_data = fn(data)
        tmp = path + ".tmp"
        with open(tmp, "w") as tf:
            json.dump(new_data, tf, indent=2, ensure_ascii=False)
            tf.flush()
            os.fsync(tf.fileno())
        os.rename(tmp, path)
    return new_data


def read_profile() -> Dict[str, Any]:
    """Return the family profile dictionary (creates file if missing)."""
    return _read_json(PROFILE_PATH)


def append_fact(note: str) -> None:
    """Add a critical note to the family profile under lock."""
    def update(profile):
        notes = profile.get("critical_notes", [])
        if not isinstance(notes, list):
            notes = []
        notes.append(note)
        profile["critical_notes"] = notes
        return profile
    _update_json(PROFILE_PATH, update)


def read_calendar() -> List[Dict[str, Any]]:
    """Return list of calendar events."""
    return _read_json(CALENDAR_PATH)


def add_event(title: str, timestamp_iso: str, source_email_id: int | None = None) -> Dict[str, Any]:
    """Add a new event and return its dict.

    If *source_email_id* is provided, it references the ``emails.id``
    column in ``storage/hozbot.db`` so the event can be traced back
    to the email that triggered it.
    """
    event = {
        "id": str(uuid.uuid4()),
        "title": title,
        "timestamp": timestamp_iso,
        "reminder_sent": False,
    }
    if source_email_id is not None:
        event["source_email_id"] = source_email_id
    def update(events):
        events.append(event)
        return events
    _update_json(CALENDAR_PATH, update)
    return event


def mark_event_sent(event_id: str) -> None:
    """Mark a calendar event as reminder sent."""
    def update(events):
        for ev in events:
            if ev.get("id") == event_id:
                ev["reminder_sent"] = True
                break
        return events
    _update_json(CALENDAR_PATH, update)
