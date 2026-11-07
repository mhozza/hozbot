import json
import os
import fcntl
from typing import List, Dict, Any, Callable

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PROFILE_PATH = os.path.join(BASE_DIR, "storage", "family_profile.json")
CALENDAR_PATH = os.path.join(BASE_DIR, "storage", "calendar_db.json")


def _ensure_dir(path: str) -> None:
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, mode=0o700, exist_ok=True)
        try:
            os.chmod(dir_path, 0o700)
        except OSError:
            pass


def _default_for(path: str) -> Any:
    """Return a sensible default for a known storage path."""
    return [] if path.endswith("_db.json") else {}


def _read_json(path: str) -> Any:
    """Read a JSON file under a shared (non‑exclusive) lock.

    Returns the default value for missing, empty, or corrupt files.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
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
    _ensure_dir(path)
    if not os.path.exists(path):
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    else:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    with open(path, "r+", encoding="utf-8") as f:
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
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with open(fd, "w", encoding="utf-8") as tf:
                json.dump(new_data, tf, indent=2, ensure_ascii=False)
                tf.flush()
                os.fsync(tf.fileno())
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.rename(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return new_data


def read_profile() -> Dict[str, Any]:
    """Return the family profile dictionary (creates file if missing)."""
    if not os.path.exists(PROFILE_PATH):
        _ensure_dir(PROFILE_PATH)
        _update_json(PROFILE_PATH, lambda d: d)
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



