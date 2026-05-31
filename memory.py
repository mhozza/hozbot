import json
import os
from datetime import datetime, timezone, timedelta
from typing import List, Dict

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
if not os.path.isdir(STORAGE_DIR):
    os.makedirs(STORAGE_DIR, exist_ok=True)
MEMORY_FILE = os.path.join(STORAGE_DIR, "thread_memory.json")

MEMORY_WINDOW_MINUTES = 60
MAX_MESSAGES = 100


def _load_memory() -> Dict[str, list]:
    if not os.path.exists(MEMORY_FILE):
        return {}
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    migrated = False
    for user_key, msgs in data.items():
        if msgs and isinstance(msgs[0], str):
            now_ts = datetime.now(timezone.utc).isoformat()
            data[user_key] = [{"text": m, "ts": now_ts} for m in msgs]
            migrated = True
    if migrated:
        _save_memory(data)
    return data


def _save_memory(data: Dict[str, list]) -> None:
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_memory(user_id: int) -> List[str]:
    data = _load_memory()
    raw = data.get(str(user_id), [])
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=MEMORY_WINDOW_MINUTES)
    return [
        entry["text"]
        for entry in raw
        if datetime.fromisoformat(entry["ts"]) >= cutoff
    ]


def add_message(user_id: int, message: str) -> None:
    data = _load_memory()
    user_key = str(user_id)
    msgs = data.get(user_key, [])
    msgs.append({"text": message, "ts": datetime.now(timezone.utc).isoformat()})
    if len(msgs) > MAX_MESSAGES:
        msgs = msgs[-MAX_MESSAGES:]
    data[user_key] = msgs
    _save_memory(data)


def clear_memory(user_id: int) -> None:
    data = _load_memory()
    user_key = str(user_id)
    if user_key in data:
        del data[user_key]
        _save_memory(data)


def clear_memory_before(user_id: int, before_ts: str) -> None:
    data = _load_memory()
    user_key = str(user_id)
    msgs = data.get(user_key, [])
    if not msgs:
        return
    try:
        before_dt = datetime.fromisoformat(before_ts)
    except ValueError:
        return
    kept = [m for m in msgs if datetime.fromisoformat(m["ts"]) >= before_dt]
    if kept:
        data[user_key] = kept
    else:
        del data[user_key]
    _save_memory(data)
