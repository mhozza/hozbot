#!/usr/bin/env python3
"""One-time Google Calendar OAuth setup.

Runs the interactive OAuth flow, saves the token, and optionally
migrates existing calendar_db.json events to Google Calendar.

Usage:
    uv run tools/auth_google_calendar.py
    uv run tools/auth_google_calendar.py --email bot@example.com
"""

import os
import sys
import argparse
import logging
from dotenv import load_dotenv

# Ensure project root is on sys.path so we can import sibling modules
_project_root = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

load_dotenv()

logging.basicConfig(level=logging.INFO)

parser = argparse.ArgumentParser(description="Google Calendar OAuth setup")
parser.add_argument("--email", help="Email to pre-fill in the OAuth page (for multiple-account setups)")
args = parser.parse_args()

# Resolve paths the same way main.py does
base = os.path.dirname(os.path.abspath(__file__))
cred = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_FILE", "storage/credentials.json")
tok = os.getenv("GOOGLE_CALENDAR_TOKEN_FILE", "storage/google_calendar_token.json")


def resolve(p: str) -> str:
    if not os.path.isabs(p):
        p = os.path.join(base, "..", p)
    return os.path.normpath(p)


creds_path = resolve(cred)
token_path = resolve(tok)

from google_calendar import GoogleCalendar, migrate_from_json

gc = GoogleCalendar(creds_path, token_path)
gc.auth_console(login_hint=args.email)
print(f"✓ Authenticated! Token saved to {token_path}")

# Check for old JSON calendar to migrate
import database
if os.path.exists(database.CALENDAR_PATH):
    count = migrate_from_json(database.CALENDAR_PATH, gc)
    if count > 0:
        print(f"✓ Migrated {count} existing events to Google Calendar")
    else:
        print("No events to migrate.")
else:
    print("No existing calendar_db.json found — nothing to migrate.")
