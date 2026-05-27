import json
import os
import uuid
from datetime import datetime
from typing import List, Dict, Any

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PROFILE_PATH = os.path.join(BASE_DIR, "storage", "family_profile.json")
CALENDAR_PATH = os.path.join(BASE_DIR, "storage", "calendar_db.json")

def _load_json(path: str) -> Any:
    if not os.path.exists(path):
        # Initialize empty file
        with open(path, "w", encoding="utf-8") as f:
            f.write("[]" if path.endswith("_db.json") else "{}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def read_profile() -> Dict[str, Any]:
    """Return the family profile dictionary (creates file if missing)."""
    return _load_json(PROFILE_PATH)

def write_profile(profile: Dict[str, Any]) -> None:
    _write_json(PROFILE_PATH, profile)

def append_fact(note: str) -> None:
    profile = read_profile()
    notes = profile.get("critical_notes", [])
    if not isinstance(notes, list):
        notes = []
    notes.append(note)
    profile["critical_notes"] = notes
    write_profile(profile)

def read_calendar() -> List[Dict[str, Any]]:
    """Return list of calendar events, each event is a dict with id, title, timestamp, reminder_sent."""
    return _load_json(CALENDAR_PATH)

def write_calendar(events: List[Dict[str, Any]]) -> None:
    _write_json(CALENDAR_PATH, events)

def add_event(title: str, timestamp_iso: str) -> Dict[str, Any]:
    events = read_calendar()
    event_id = str(uuid.uuid4())
    event = {
        "id": event_id,
        "title": title,
        "timestamp": timestamp_iso,
        "reminder_sent": False,
    }
    events.append(event)
    write_calendar(events)
    return event

def mark_event_sent(event_id: str) -> None:
    events = read_calendar()
    for ev in events:
        if ev.get("id") == event_id:
            ev["reminder_sent"] = True
            break
    write_calendar(events)
