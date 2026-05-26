import json
import os
from typing import List, Dict

# Define path for thread memory storage (JSON file)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
MEMORY_FILE = os.path.join(BASE_DIR, "thread_memory.json")

def _load_memory() -> Dict[str, List[str]]:
    """Load the memory JSON file. Returns a dict mapping user_id (as string) to list of messages."""
    if not os.path.exists(MEMORY_FILE):
        return {}
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # If corrupted, start fresh
        return {}

def _save_memory(data: Dict[str, List[str]]) -> None:
    """Write the memory dict to the JSON file."""
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_memory(user_id: int) -> List[str]:
    """Retrieve the thread memory for a given user. Returns list of messages (oldest to newest)."""
    data = _load_memory()
    return data.get(str(user_id), [])

def add_message(user_id: int, message: str) -> None:
    """Append a message to the user's thread memory."""
    data = _load_memory()
    user_key = str(user_id)
    msgs = data.get(user_key, [])
    msgs.append(message)
    data[user_key] = msgs
    _save_memory(data)

def clear_memory(user_id: int) -> None:
    """Clear all stored messages for the given user."""
    data = _load_memory()
    user_key = str(user_id)
    if user_key in data:
        del data[user_key]
        _save_memory(data)
