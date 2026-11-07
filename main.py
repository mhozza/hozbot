import os
import mimetypes
import logging
import httpx
from dataclasses import dataclass
from dotenv import load_dotenv
from string import Template
# Load environment variables first to avoid credential errors during initialization
load_dotenv()
# Retrieve Gemini API key for PydanticAI (used automatically by the provider)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

from pydantic_ai import Agent, RunContext, BinaryContent
from pydantic_ai.exceptions import ModelHTTPError
from telegram import Update, Message, Bot, LinkPreviewOptions, constants
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, Defaults
from telegram_utils import sanitize_telegram_html
import agent_email
import email_store
from pydantic_ai.capabilities import Thinking
import asyncio
from datetime import date, datetime, time, timezone, timedelta
from zoneinfo import ZoneInfo
from database import read_profile, append_fact
import json
from typing import List
import memory
from dataclasses import field
import bin_collection
# Configure logging
def is_debug_mode(environment: str | None = None, debug: bool | str | None = None) -> bool:
    """Determine whether debug/development mode is active.

    Explicit arguments take precedence over environment variables.
    Defaults to False (release mode) if unspecified.
    """
    if debug is not None:
        if isinstance(debug, bool):
            return debug
        return str(debug).strip().lower() in ("1", "true", "yes", "t", "on", "dev", "development", "debug")

    if environment is not None:
        return environment.strip().lower() in ("development", "dev", "debug", "test", "testing", "local")

    debug_env = os.getenv("DEBUG")
    if debug_env is not None:
        return debug_env.strip().lower() in ("1", "true", "yes", "t", "on", "dev", "development", "debug")

    env_val = os.getenv("ENVIRONMENT") or os.getenv("ENV")
    if env_val is not None:
        return env_val.strip().lower() in ("development", "dev", "debug", "test", "testing", "local")

    return False


def configure_logging(environment: str | None = None, debug: bool | str | None = None) -> bool:
    """Configure logging for hozbot, httpx, and httpcore.

    In release mode (default):
    - Suppress INFO request logging for httpx and httpcore (set to WARNING) to prevent
      plaintext TELEGRAM_BOT_TOKEN exposure in Telegram API polling logs.
    - Set root and application loggers to INFO.
    - Downgrade full LLM response logs to DEBUG so private schedule/email details are suppressed.

    In development/debug mode:
    - Set root and application loggers to DEBUG.
    - Set httpx and httpcore loggers to DEBUG, retaining verbose HTTP request logs.
    - Full LLM response logs are emitted.

    Returns:
        bool: True if debug/development mode is active, False if release mode.
    """
    is_debug = is_debug_mode(environment=environment, debug=debug)
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    target_level = logging.DEBUG if is_debug else logging.INFO

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(format=log_format, level=target_level)
    else:
        root_logger.setLevel(target_level)

    if is_debug:
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        logging.getLogger("httpcore").setLevel(logging.DEBUG)
        logging.getLogger(__name__).setLevel(logging.DEBUG)
    else:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger(__name__).setLevel(logging.INFO)

    return is_debug


configure_logging()
logger = logging.getLogger(__name__)

# Parse whitelisted user IDs
ALLOWED_USER_IDS_STR = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = [
    int(uid.strip())
    for uid in ALLOWED_USER_IDS_STR.split(",")
    if uid.strip().isdigit()
]
memory.BROADCAST_USER_IDS = ALLOWED_USER_IDS

# Parse send hi/bye user IDs (separate from the auth whitelist)
SEND_HI_BYE_STR = os.getenv("SEND_HI_BYE", "")
SEND_HI_BYE_USER_IDS = [
    int(uid.strip())
    for uid in SEND_HI_BYE_STR.split(",")
    if uid.strip().isdigit()
]

SEND_DIGEST_ON_START = os.getenv("SEND_DIGEST_ON_START", "false").lower() == "true"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")
DOWNLOADS_DIR = os.path.realpath(os.path.join(BASE_DIR, "downloads"))
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

LAST_DIGEST_PATH = os.path.join(BASE_DIR, "storage", "last_digest.json")
SKIPPED_EVENTS_PATH = os.path.join(BASE_DIR, "storage", "skipped_events.json")

# Retry behaviour for transient model/provider errors (e.g. 503 Service Unavailable)
RETRY_STATUS_CODES = {503, 429, 500, 502, 504}
RETRY_INTERVAL_MINUTES = 10
MAX_ATTEMPTS = 6
DIGEST_RETRY_ATTEMPT = 1

# Global Telegram bot reference so agent tools can send files to users
_telegram_bot: Bot | None = None
_pending_attachment: str | None = None  # set by download_attachment for image follow-up


def read_last_digest_time() -> str | None:
    try:
        with open(LAST_DIGEST_PATH) as f:
            return json.load(f).get("last_digest_at")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_last_digest_time(timestamp_iso: str) -> None:
    os.makedirs(os.path.dirname(LAST_DIGEST_PATH), exist_ok=True)
    with open(LAST_DIGEST_PATH, "w") as f:
        json.dump({"last_digest_at": timestamp_iso}, f)


async def safe_reply(message: Message, text: str) -> Message:
    """Send a reply with HTML parse mode, sanitizing unsupported tags and disabling link previews."""
    return await message.reply_text(
        sanitize_telegram_html(text),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def safe_send(bot, chat_id: int, text: str) -> Message:
    """Send a proactive message with HTML parse mode, sanitizing unsupported tags and disabling link previews."""
    return await bot.send_message(
        chat_id=chat_id,
        text=sanitize_telegram_html(text),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@dataclass
class FamilySystemContext:
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    thread_memory: List[str] = field(default_factory=list)

# Initialize PydanticAI Agent with "Chief of Staff" corporate persona
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

from pydantic_ai.models.fallback import FallbackModel
from google_calendar import GoogleCalendar, migrate_from_json, _parse_iso, LOCAL_TZ, LOCAL_TZ_NAME, _event_start_raw
import database as db_mod
import event_store

# Initialize Google Calendar client
GOOGLE_CALENDAR_CREDS = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_FILE", os.path.join(BASE_DIR, "storage", "credentials.json"))
GOOGLE_CALENDAR_TOKEN = os.getenv("GOOGLE_CALENDAR_TOKEN_FILE", os.path.join(BASE_DIR, "storage", "google_calendar_token.json"))
gc = GoogleCalendar(GOOGLE_CALENDAR_CREDS, GOOGLE_CALENDAR_TOKEN)


def _parse_iso_for_digest(iso_str: str) -> datetime:
    return _parse_iso(iso_str)


def _format_local_time(iso_str: str) -> str:
    """Parse an ISO datetime string and return a human-readable local time.

    Converts to the local timezone (TIMEZONE env var, e.g. Europe/London)
    so that UTC times are shown with the correct offset (BST in summer).
    For date-only strings (e.g. all-day events), returns the date without time.
    """
    if not iso_str or iso_str == "?":
        return iso_str
    try:
        if "T" not in iso_str and " " not in iso_str:
            d = date.fromisoformat(iso_str)
            return d.strftime("%a %d %b")
        dt = _parse_iso(iso_str)
        return dt.astimezone(LOCAL_TZ).strftime("%a %d %b, %H:%M")
    except (ValueError, TypeError) as e:
        logger.warning("Failed to format local time for %r: %s", iso_str, e)
        return iso_str



# Initialize Google provider with API key from environment
google_provider = GoogleProvider(api_key=os.getenv("GOOGLE_API_KEY"))
# Initialize GoogleModel instances for primary and fallback
primary_model = GoogleModel('gemini-3.8-flash', provider=google_provider)
middle_model = GoogleModel('gemini-3.7-flash', provider=google_provider)
fallback_model = GoogleModel('gemini-3.5-flash-lite', provider=google_provider)
# Use FallbackModel to try primary first, then fallbacks
model = FallbackModel(primary_model, middle_model, fallback_model)

# Load prompt templates
SYSTEM_PROMPT = open(os.path.join(PROMPTS_DIR, "system_prompt.md")).read().strip()
EMAIL_CHECK_TEMPLATE = Template(open(os.path.join(PROMPTS_DIR, "email_check.md")).read().strip())
EVENING_DIGEST_TEMPLATE = Template(open(os.path.join(PROMPTS_DIR, "evening_digest.md")).read().strip())

# Initialize PydanticAI Agent with GoogleModel instance for interactive user chat
agent = Agent(
    model,
    deps_type=FamilySystemContext,
    capabilities=[Thinking(effort='low')],
    system_prompt=SYSTEM_PROMPT,
)

# Dedicated isolated agent for unattended background email analysis (SEC-01)
# Restricts available tools to safe extraction/querying tools only.
# Specifically excludes dangerous/exfiltrating/poisoning tools:
# remember_this, delete_calendar_event, update_calendar_event, remove_email_event,
# send_file_to_chat, extract_pdf_file, download_attachment, search_stored_emails, etc.
EMAIL_AGENT_SYSTEM_PROMPT = (
    "You are an automated email analysis assistant for a family office. "
    "Your objective is to analyze unread emails from the shared inbox, extract dates, "
    "events, and deadlines, store them in the local database, sync relevant events to "
    "Google Calendar, and surface urgent items."
)
email_agent = Agent(
    model,
    deps_type=FamilySystemContext,
    capabilities=[Thinking(effort='low')],
    system_prompt=EMAIL_AGENT_SYSTEM_PROMPT,
)

@agent.system_prompt
def add_recent_conversation(ctx: RunContext[FamilySystemContext]) -> str:
    msgs = ctx.deps.thread_memory
    if not msgs:
        return ""
    lines = ["\n# Recent conversation history"]
    for i, msg in enumerate(msgs):
        speaker = "User" if i % 2 == 0 else "Hozbot"
        lines.append(f"  {speaker}: {msg}")
    return "\n".join(lines)

@agent.system_prompt
@email_agent.system_prompt
def add_timezone_info() -> str:
    now = datetime.now(LOCAL_TZ)
    off = now.strftime("%z")
    offset_iso = f"{off[:3]}:{off[3:]}"
    return f"\n# Timezone\nThe bot operates in {LOCAL_TZ_NAME} (current UTC offset {offset_iso}). All dates and times mentioned are in this timezone unless otherwise specified."

@agent.system_prompt
@email_agent.system_prompt
def add_profile_context() -> str:
    profile = read_profile()
    notes = profile.get("critical_notes", [])
    if not notes:
        return ""
    lines = ["\n# Family profile (learned preferences & facts)"]
    for i, note in enumerate(notes, 1):
        lines.append(f"  {i}. {note}")
    return "\n".join(lines)

@agent.tool
def check_shared_inbox(ctx: RunContext[FamilySystemContext]) -> str:
    """Fetch and summarize unread emails in the shared family office inbox.
    
    Returns a detailed summary of unread emails, including UID, sender, subject,
    body snippet, and any attachments with their filenames.
    Use the returned UID and attachment filename with the download_attachment tool.
    """
    try:
        emails = agent_email.fetch_unread_emails()
        if not emails:
            return "No unread emails found."
        
        for email_data in emails:
            email_store.store_email(email_data)

        email_summaries = []
        for idx, email_data in enumerate(emails, 1):
            summary = (
                f"[{idx}] UID: {email_data['uid']}\n"
                f"From: {email_data['sender']}\n"
                f"Subject: {email_data['subject']}\n"
                f"Content: {email_data['body_snippet']}\n"
            )
            if email_data['attachments']:
                att_lines = [f"  - {a['filename']} ({a['mime_type']}, {a['size_bytes']} bytes)" for a in email_data['attachments']]
                summary += "Attachments:\n" + "\n".join(att_lines) + "\n"
            email_summaries.append(summary)
        return "\n".join(email_summaries)
    except Exception as e:
        logger.error(f"Error in check_shared_inbox tool: {e}", exc_info=True)
        return f"Error retrieving emails: {str(e)}"

from agent_email import (
    extract_pdf_text,
    fetch_attachment_content_by_uid,
    DEFAULT_MAX_PDF_PAGES,
    DEFAULT_MAX_PDF_CHARS,
    DEFAULT_MAX_PDF_SIZE_BYTES,
    DEFAULT_IMAP_TIMEOUT,
    DEFAULT_MAX_UNREAD_EMAILS,
)

# Resource limits and poison-pill protection (SEC-05)
MAX_DOCUMENT_SIZE_BYTES = DEFAULT_MAX_PDF_SIZE_BYTES  # 20MB
MAX_ATTACHMENT_SIZE_BYTES = DEFAULT_MAX_PDF_SIZE_BYTES  # 20MB
MAX_EMAIL_FAILURES = 3
_email_failure_counts: dict[str, int] = {}

@agent.tool
@email_agent.tool
def get_profile(ctx: RunContext[FamilySystemContext]) -> str:
    """Return the family profile JSON as a formatted string."""
    profile = read_profile()
    return json.dumps(profile, indent=2)

@agent.tool
def remember_this(ctx: RunContext[FamilySystemContext], note: str) -> str:
    """Store a note, preference, or fact for future reference.

    Use this when the user tells you something they want you to remember —
    e.g. what they care about, what to ignore, response style preferences,
    or anything that should influence future behaviour.
    These notes are shown to you on every conversation turn.
    """
    append_fact(note)
    return "Noted."

@agent.tool
def get_calendar(ctx: RunContext[FamilySystemContext]) -> str:
    """Return the list of upcoming calendar events (next 90 days) from all accessible calendars."""
    try:
        now = datetime.now(timezone.utc)
        events = gc.list_all_events(now, now + timedelta(days=90))
        if not events:
            return "No events scheduled."
        lines = ["Upcoming events (next 90 days):"]
        for ev in events:
            title = ev.get("summary", "Untitled")
            raw = _event_start_raw(ev) or "?"
            event_id = ev.get("id", "?")
            cal = ev.get("_calendar_summary", "primary")
            lines.append(f"- [{cal}] {title} at {_format_local_time(raw)} (ID: {event_id})")
        return "\n".join(lines)
    except Exception as e:
        logger.error("Error in get_calendar tool: %s", e, exc_info=True)
        return f"Error retrieving calendar: {str(e)}"

@agent.tool
def add_calendar_event(ctx: RunContext[FamilySystemContext], title: str, start_iso: str, end_iso: str | None = None) -> str:
    """Add a new event directly to the Google Calendar.

    - title: Event title/summary
    - start_iso: ISO 8601 start datetime in the local timezone (e.g. "2026-06-23T09:00:00+01:00"). If timezone is omitted, local timezone is assumed.
    - end_iso: Optional ISO 8601 end datetime. If omitted, defaults to 1 hour after start.
    """
    try:
        event = gc.create_event(summary=title, start_iso=start_iso, end_iso=end_iso)
        return f"Event added with ID {event.get('id')}"
    except Exception as e:
        logger.error("Error in add_calendar_event tool: %s", e, exc_info=True)
        return f"Error adding event: {str(e)}"

@agent.tool
def delete_calendar_event(ctx: RunContext[FamilySystemContext], event_id: str) -> str:
    """Delete a calendar event by its Google Calendar event ID.

    Use the ID shown when listing events with get_calendar.
    If this event was originally synced from an email extraction,
    it will also be removed from the local database.
    """
    try:
        gc.delete_event(event_id)
        ev = event_store.get_event_by_gcal_id(event_id)
        if ev:
            event_store.delete_event(ev["id"])
        return f"Event {event_id} deleted."
    except Exception as e:
        logger.error("Error in delete_calendar_event tool: %s", e, exc_info=True)
        return f"Error deleting event: {str(e)}"

@agent.tool
def update_calendar_event(ctx: RunContext[FamilySystemContext], event_id: str, title: str | None = None, start_iso: str | None = None, end_iso: str | None = None) -> str:
    """Update an existing Google Calendar event by its Google Calendar event ID.

    Only provided fields will be updated. Omit fields you don't want to change.
    Use the ID shown when listing events with get_calendar.
    Note: this only updates Google Calendar, not the local email events database.
    """
    try:
        gc.update_event(
            event_id=event_id,
            summary=title,
            start_iso=start_iso,
            end_iso=end_iso,
        )
        return f"Event {event_id} updated."
    except Exception as e:
        logger.error("Error in update_calendar_event tool: %s", e, exc_info=True)
        return f"Error updating event: {str(e)}"

@agent.tool
@email_agent.tool
def add_email_event(ctx: RunContext[FamilySystemContext], title: str, start_iso: str, end_iso: str | None = None, email_uid: str | None = None, sync_to_gcal: bool = False) -> str:
    """Store an event extracted from an email in the local database.

    Use this for ALL dates found in emails. If the event is relevant to the family
    based on the profile, set sync_to_gcal=True to also publish it to Google Calendar.

    - title: Event title/summary
    - start_iso: ISO 8601 start datetime in the local timezone (e.g. "2026-06-23T09:00:00+01:00"). If timezone is omitted, local timezone is assumed.
    - end_iso: Optional ISO 8601 end datetime. If omitted, defaults to None.
    - email_uid: The UID of the email this event was extracted from.
    - sync_to_gcal: If True, also creates this event in Google Calendar.
    """
    try:
        source_email_id = None
        if email_uid:
            email_data = email_store.get_email_by_uid(email_uid)
            if email_data:
                source_email_id = email_data["id"]
        event_id = event_store.add_event(title, start_iso, end_iso, source_email_id)
        if sync_to_gcal:
            gcal_event = gc.create_event(
                summary=title,
                start_iso=start_iso,
                end_iso=end_iso,
                source_email_id=source_email_id,
            )
            gcal_id = gcal_event.get("id")
            event_store.mark_synced(event_id, gcal_id)
            return f"Event stored with local ID {event_id} and synced to Google Calendar (ID: {gcal_id})"
        return f"Event stored with local ID {event_id} (local only — use sync_email_event_to_gcal to publish)"
    except Exception as e:
        logger.error("Error in add_email_event tool: %s", e, exc_info=True)
        return f"Error adding email event: {str(e)}"

@agent.tool
@email_agent.tool
def sync_email_event_to_gcal(ctx: RunContext[FamilySystemContext], event_id: int) -> str:
    """Publish a locally-stored email event to Google Calendar.

    Use this when the AI skipped syncing an event and the user wants it published.
    Returns an error if the event is already synced.

    - event_id: The local database ID returned by add_email_event.
    """
    try:
        ev = event_store.get_event(event_id)
        if not ev:
            return f"Event {event_id} not found."
        if ev["synced_to_gcal"] and ev["google_event_id"]:
            return f"Event {event_id} is already synced to Google Calendar (ID: {ev['google_event_id']})"
        ev_title = ev["title"]
        ev_start = ev["start_iso"]
        ev_end = ev["end_iso"]
        gcal_event = gc.create_event(
            summary=ev_title,
            start_iso=ev_start,
            end_iso=ev_end,
            source_email_id=ev["source_email_id"],
        )
        gcal_id = gcal_event.get("id")
        event_store.mark_synced(event_id, gcal_id)
        return f"Event {event_id} synced to Google Calendar (ID: {gcal_id})"
    except Exception as e:
        logger.error("Error in sync_email_event_to_gcal tool: %s", e, exc_info=True)
        return f"Error syncing event: {str(e)}"

@agent.tool
@email_agent.tool
def list_email_events(ctx: RunContext[FamilySystemContext], date: str | None = None, title: str | None = None, check_gcal: bool = False) -> str:
    """List upcoming email-extracted events from the local database.
    Optionally filter by date (YYYY-MM-DD) and/or title (substring match, case-insensitive).
    Shows sync status: ✅ = synced to Google Calendar, 📋 = local only.
    When check_gcal=True, also searches events across ALL shared Google Calendars and includes them.
    """
    try:
        events = event_store.get_future_events(days=90)
        if date:
            events = [ev for ev in events if ev["start_iso"].startswith(date)]
        if title:
            title_lower = title.lower()
            events = [ev for ev in events if title_lower in ev["title"].lower()]

        lines = ["Email-extracted events:"]
        for ev in events:
            badge = "✅" if ev["synced_to_gcal"] else "📋"
            lines.append(f"- {badge} {ev['title']} at {ev['start_iso']} (ID: {ev['id']})")

        if check_gcal and (date or title):
            now = datetime.now(timezone.utc)
            gcal_events = gc.list_all_events(now, now + timedelta(days=90))
            if date:
                gcal_events = [ev for ev in gcal_events if _event_start_raw(ev).startswith(date)]
            if title:
                title_lower = title.lower()
                gcal_events = [ev for ev in gcal_events if title_lower in ev.get("summary", "").lower()]
            if gcal_events:
                lines.append("\nMatching events on shared calendars:")
                for ev in gcal_events:
                    title_s = ev.get("summary", "Untitled")
                    start_s = _event_start_raw(ev) or "?"
                    cal = ev.get("_calendar_summary", "?")
                    lines.append(f"- [{cal}] {title_s} at {start_s}")

        if len(lines) == 1:
            return "No email-extracted events matching your criteria."
        return "\n".join(lines)
    except Exception as e:
        logger.error("Error in list_email_events tool: %s", e, exc_info=True)
        return f"Error listing email events: {str(e)}"

@agent.tool
def remove_email_event(ctx: RunContext[FamilySystemContext], event_id: int) -> str:
    """Delete an email-extracted event from the local database.

    If it was also synced to Google Calendar, it will be removed from there too.

    - event_id: The local database ID returned by add_email_event.
    """
    try:
        ev = event_store.get_event(event_id)
        if not ev:
            return f"Event {event_id} not found."
        if ev["synced_to_gcal"] and ev["google_event_id"]:
            try:
                gc.delete_event(ev["google_event_id"])
            except Exception:
                pass
        event_store.delete_event(event_id)
        return f"Event {event_id} removed."
    except Exception as e:
        logger.error("Error in remove_email_event tool: %s", e, exc_info=True)
        return f"Error removing event: {str(e)}"

@agent.tool
@email_agent.tool
def update_email_event(ctx: RunContext[FamilySystemContext], event_id: int, title: str | None = None, start_iso: str | None = None, end_iso: str | None = None) -> str:
    """Update an existing email-extracted event's title, start, and/or end time.
    If the event was synced to Google Calendar, the change is pushed there too.

    - event_id: The local database ID of the event to update.
    - title: New title (optional — omit to keep current).
    - start_iso: New start ISO datetime (optional).
    - end_iso: New end ISO datetime (optional).
    """
    try:
        ev = event_store.get_event(event_id)
        if not ev:
            return f"Event {event_id} not found."
        event_store.update_event(event_id, title=title, start_iso=start_iso, end_iso=end_iso)
        if ev["synced_to_gcal"] and ev["google_event_id"]:
            gc.update_event(
                event_id=ev["google_event_id"],
                summary=title or ev["title"],
                start_iso=start_iso or ev["start_iso"],
                end_iso=end_iso or ev["end_iso"],
            )
        return f"Event {event_id} updated."
    except Exception as e:
        logger.error("Error in update_email_event tool: %s", e, exc_info=True)
        return f"Error updating event: {str(e)}"

@agent.tool
@email_agent.tool
def note_event_skipped(ctx: RunContext[FamilySystemContext], title: str, date: str, reason: str = "already exists on a shared calendar") -> str:
    """Record an event that was found in an email but not added because it already exists elsewhere.

    This is used to track events skipped during email processing so the evening
    digest can mention them.

    - title: Event title/summary
    - date: Event date (YYYY-MM-DD)
    - reason: Why it was skipped (defaults to "already exists on a shared calendar")
    """
    try:
        skipped: list[dict] = []
        if os.path.exists(SKIPPED_EVENTS_PATH):
            with open(SKIPPED_EVENTS_PATH) as f:
                skipped = json.load(f)
        skipped.append({
            "title": title,
            "date": date,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        os.makedirs(os.path.dirname(SKIPPED_EVENTS_PATH), exist_ok=True)
        with open(SKIPPED_EVENTS_PATH, "w") as f:
            json.dump(skipped, f, indent=2)
        return f"Skipped event noted: {title} on {date} ({reason})"
    except Exception as e:
        logger.error("Error in note_event_skipped tool: %s", e, exc_info=True)
        return f"Error noting skipped event: {str(e)}"

@agent.tool
def clear_thread_memory(ctx: RunContext[FamilySystemContext], before: str | None = None) -> str:
    """Clear stored thread memory for the current user.

    Args:
        before: Optional ISO timestamp string. If provided, clears only
                messages older than this timestamp. If omitted, clears
                all thread memory.
    """
    if before:
        memory.clear_memory_before(ctx.deps.user_id, before)
        return f"Thread memory older than {before} cleared."
    memory.clear_memory(ctx.deps.user_id)
    return "Thread memory cleared."

@agent.tool_plain
@email_agent.tool_plain
def get_current_datetime() -> str:
    """Return the current date and time in the local timezone."""
    now = datetime.now(LOCAL_TZ)
    return now.strftime("%A, %Y-%m-%d %H:%M:%S %Z")

@agent.tool
def search_stored_emails(ctx: RunContext[FamilySystemContext], query: str, limit: int = 20) -> str:
    """Search through stored emails by sender, subject, or body content.

    Returns matching email IDs with sender, subject, date, and a snippet.
    Use the returned UID to reference emails in other tools.
    """
    try:
        results = email_store.search_emails(query, limit=limit)
        if not results:
            return "No stored emails match your query."
        parts = [f"Found {len(results)} matching email(s):"]
        for r in results:
            att_line = ""
            if r.get("attachments"):
                att_line = f"\n       Attachments: {', '.join(a['filename'] for a in r['attachments'])}"
            parts.append(
                f"  • ID: {r['id']} | UID: {r['uid']}\n"
                f"    From: {r['sender']}\n"
                f"    Subject: {r['subject']}\n"
                f"    {r['received_at'] or r['fetched_at']}\n"
                f"    {r['body_snippet']}{att_line}"
            )
        return "\n\n".join(parts)
    except Exception as e:
        logger.error(f"Error in search_stored_emails tool: {e}", exc_info=True)
        return f"Error searching emails: {str(e)}"


def _resolve_download_path(file_path: str) -> str:
    """Resolve file_path canonically and ensure it resides strictly within DOWNLOADS_DIR.

    Raises ValueError if:
    - Path is empty or whitespace
    - Path resolves outside DOWNLOADS_DIR (directory traversal, absolute path outside, symlink outside)
    - Path resolves to DOWNLOADS_DIR itself or any directory
    """
    if not file_path or not str(file_path).strip():
        raise ValueError("File path cannot be empty.")

    canonical_downloads = os.path.realpath(DOWNLOADS_DIR)
    path_str = str(file_path).strip()

    if os.path.isabs(path_str):
        candidate = path_str
    elif (
        path_str.startswith("downloads/")
        or path_str.startswith("downloads\\")
        or path_str.startswith("./downloads/")
        or path_str.startswith("./downloads\\")
        or path_str == "downloads"
        or path_str == "./downloads"
    ):
        candidate = os.path.join(BASE_DIR, path_str)
    else:
        candidate = os.path.join(canonical_downloads, path_str)

    resolved = os.path.realpath(candidate)

    try:
        is_contained = os.path.commonpath([resolved, canonical_downloads]) == canonical_downloads
    except ValueError:
        is_contained = False

    if not is_contained or resolved == canonical_downloads:
        raise ValueError(f"Access denied: path '{file_path}' resolves outside authorized downloads directory.")

    if os.path.isdir(resolved):
        raise ValueError(f"Access denied: '{file_path}' is a directory, not a file.")

    return resolved


@agent.tool
def download_attachment(ctx: RunContext[FamilySystemContext], uid: str, filename: str) -> str:
    """Download a specific attachment from an email by UID and filename, save it locally, and return the file path.
    
    Checks the local cache first to avoid re-downloading from IMAP.
    """
    global _pending_attachment
    try:
        cached = email_store.get_downloaded_path(uid, filename)
        if cached:
            mime_type, _ = mimetypes.guess_type(filename)
            if mime_type and mime_type.startswith('image/'):
                _pending_attachment = cached
            return f"Already downloaded at {cached} ({os.path.getsize(cached)} bytes)"

        data = fetch_attachment_content_by_uid(uid, filename)
        if data is None:
            return f"Attachment '{filename}' not found in email UID {uid}."
        if len(data) > MAX_ATTACHMENT_SIZE_BYTES:
            logger.warning(
                "Attachment '%s' size (%d bytes) exceeds maximum allowed limit (%d bytes)",
                filename,
                len(data),
                MAX_ATTACHMENT_SIZE_BYTES,
            )
            return f"Error: Attachment '{filename}' size ({len(data)} bytes) exceeds maximum limit ({MAX_ATTACHMENT_SIZE_BYTES} bytes)."
        safe_filename = os.path.basename(filename)
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        file_path = os.path.join(DOWNLOADS_DIR, f"{uid}_{safe_filename}")
        with open(file_path, "wb") as f:
            f.write(data)

        email_data = email_store.get_email_by_uid(uid)
        if email_data:
            email_store.update_attachment_local_path(email_data["id"], filename, file_path)

        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type and mime_type.startswith('image/'):
            _pending_attachment = file_path

        return f"Saved to {file_path} ({len(data)} bytes)"
    except Exception as e:
        logger.error(f"Error downloading attachment: {e}", exc_info=True)
        return f"Error downloading attachment: {str(e)}"

@agent.tool
def extract_pdf_file(ctx: RunContext[FamilySystemContext], file_path: str) -> str:
    """Extract all text from a PDF file at the given path.
    
    The file must reside within the downloads directory.
    """
    try:
        resolved_path = _resolve_download_path(file_path)
    except ValueError as e:
        logger.warning("Rejected extract_pdf_file path '%s': %s", file_path, e)
        return f"Error: {str(e)}"

    if not os.path.exists(resolved_path) or not os.path.isfile(resolved_path):
        return f"File not found: {file_path}"

    try:
        file_size = os.path.getsize(resolved_path)
        if file_size > MAX_DOCUMENT_SIZE_BYTES:
            logger.warning(
                "Rejected extract_pdf_file: file '%s' size (%d bytes) exceeds limit (%d bytes)",
                file_path,
                file_size,
                MAX_DOCUMENT_SIZE_BYTES,
            )
            return f"Error: PDF file size ({file_size} bytes) exceeds maximum allowed limit ({MAX_DOCUMENT_SIZE_BYTES} bytes)."

        with open(resolved_path, "rb") as f:
            pdf_bytes = f.read()
        text = extract_pdf_text(pdf_bytes)
        if not text:
            return "No text could be extracted from the PDF."
        return text
    except Exception as e:
        logger.error(f"Error extracting PDF text: {e}", exc_info=True)
        return f"Error extracting PDF text: {str(e)}"

@agent.tool
async def send_file_to_chat(ctx: RunContext[FamilySystemContext], file_path: str, caption: str | None = None) -> str:
    """Send a file (image, PDF, or other document) to the user in the Telegram chat.

    Use this when the user asks to see or receive an attachment from an email.
    First use download_attachment to save the file, then use this tool to send it to the user.
    The file must reside within the downloads directory.

    Args:
        file_path: Path to the file to send (as returned by download_attachment).
        caption: Optional text caption to accompany the file.
    """
    if _telegram_bot is None:
        return "Error: Telegram bot not available."
    chat_id = ctx.deps.user_id
    if chat_id == 0:
        return "Error: Cannot send files in system context."

    try:
        resolved_path = _resolve_download_path(file_path)
    except ValueError as e:
        logger.warning("Rejected send_file_to_chat path '%s': %s", file_path, e)
        return f"Error: {str(e)}"

    if not os.path.exists(resolved_path) or not os.path.isfile(resolved_path):
        return f"File not found: {file_path}"

    try:
        with open(resolved_path, "rb") as f:
            await _telegram_bot.send_document(
                chat_id=chat_id,
                document=f,
                caption=caption,
                read_timeout=30,
                write_timeout=30,
            )
        return f"Sent: {os.path.basename(resolved_path)}"
    except Exception as e:
        logger.error("Error sending file to chat: %s", e, exc_info=True)
        return f"Error sending file: {str(e)}"


@agent.tool
async def get_daily_digest(ctx: RunContext[FamilySystemContext]) -> str:
    """Generate the daily briefing with upcoming events, new emails since last digest, and events created from those emails. Does not update the digest watermark."""
    try:
        return await generate_digest_text(uid=ctx.deps.user_id)
    except Exception as e:
        logger.error(f"Error generating daily digest: {e}", exc_info=True)
        return f"Error generating daily digest: {str(e)}"

@agent.tool_plain
def check_bin_collection() -> str:
    """Check the bin collection schedule. Returns which bins go out and when.

    Reads the BIN_UPRN environment variable to look up the schedule.
    """
    return bin_collection.check_schedule()


async def run_agent(prompt, deps, uid: int | None, target_agent: Agent | None = None):
    """Run agent.run(), log prompt + response to memory if user session, return reply text."""
    active_agent = target_agent or agent
    response = await active_agent.run(prompt, deps=deps)
    reply_text = getattr(response, "output", None) or getattr(response, "data", None) or str(response)

    if uid is not None:
        if isinstance(prompt, str):
            prompt_text = prompt
        else:
            prompt_text = " ".join(str(p) for p in prompt if isinstance(p, str)) or "[Media]"
        memory.add_message(uid, prompt_text)
        memory.add_message(uid, reply_text)

    return reply_text


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _pending_attachment
    _pending_attachment = None

    # Defense-in-depth: only operate in private 1-on-1 chats to avoid leaking confidential data to groups
    chat = update.effective_chat
    if not chat or (chat.type != constants.ChatType.PRIVATE and chat.type != "private"):
        logger.warning(
            f"Non-private chat {getattr(chat, 'id', 'Unknown')} "
            f"(type: {getattr(chat, 'type', None)}) tried to send a message. Dropping immediately."
        )
        return

    # Strict ID whitelist check - drop unauthorized traffic silently
    if not update.effective_user or update.effective_user.id not in ALLOWED_USER_IDS:
        logger.warning(
            f"Unauthorized user {update.effective_user.id if update.effective_user else 'Unknown'} "
            "tried to send a message. Dropping silently."
        )
        return

    user = update.effective_user

    # Get text from either plain message or media caption
    text = update.message.text or update.message.caption or ""

    logger.info(f"Received message from authorized user {user.id} ({user.first_name})")

    # Build content for the agent (text + optional media)
    content_parts: list = []
    if text:
        content_parts.append(text)

    # Handle photo messages
    if update.message and update.message.photo:
        photo = update.message.photo[-1]
        try:
            if photo.file_size and photo.file_size > MAX_DOCUMENT_SIZE_BYTES:
                logger.warning(
                    "Photo size (%d bytes) exceeds limit of %d bytes. Skipping download.",
                    photo.file_size,
                    MAX_DOCUMENT_SIZE_BYTES,
                )
            else:
                file = await context.bot.get_file(photo.file_id)
                if file.file_size and file.file_size > MAX_DOCUMENT_SIZE_BYTES:
                    logger.warning(
                        "Photo file size (%d bytes) exceeds limit of %d bytes. Skipping download.",
                        file.file_size,
                        MAX_DOCUMENT_SIZE_BYTES,
                    )
                else:
                    image_bytes = await file.download_as_bytearray()
                    content_parts.append(BinaryContent(data=bytes(image_bytes), media_type='image/jpeg'))
                    logger.info(f"Downloaded photo {photo.file_id} ({len(image_bytes)} bytes)")
        except Exception as e:
            logger.error(f"Failed to download photo: {e}", exc_info=True)

    # Handle document messages (images, PDFs)
    if update.message and update.message.document:
        doc = update.message.document
        if doc.mime_type and (doc.mime_type.startswith('image/') or doc.mime_type == 'application/pdf'):
            try:
                if doc.file_size and doc.file_size > MAX_DOCUMENT_SIZE_BYTES:
                    logger.warning(
                        "Document %s size (%d bytes) exceeds limit of %d bytes. Skipping download.",
                        doc.file_name,
                        doc.file_size,
                        MAX_DOCUMENT_SIZE_BYTES,
                    )
                    await safe_reply(
                        update.message,
                        f"⚠️ Document '{doc.file_name or 'file'}' exceeds the maximum allowed size of 20MB and was not processed.",
                    )
                else:
                    file = await context.bot.get_file(doc.file_id)
                    if file.file_size and file.file_size > MAX_DOCUMENT_SIZE_BYTES:
                        logger.warning(
                            "Document file %s size (%d bytes) exceeds limit of %d bytes. Skipping download.",
                            doc.file_name,
                            file.file_size,
                            MAX_DOCUMENT_SIZE_BYTES,
                        )
                        await safe_reply(
                            update.message,
                            f"⚠️ Document '{doc.file_name or 'file'}' exceeds the maximum allowed size of 20MB and was not processed.",
                        )
                    else:
                        doc_bytes = await file.download_as_bytearray()
                        content_parts.append(BinaryContent(data=bytes(doc_bytes), media_type=doc.mime_type))
                        logger.info(f"Downloaded document {doc.file_name} ({len(doc_bytes)} bytes, {doc.mime_type})")
            except Exception as e:
                logger.error(f"Failed to download document: {e}", exc_info=True)

    if len(content_parts) == 0:
        return
    prompt: str | list = content_parts[0] if len(content_parts) == 1 else content_parts

    # Construct the FamilySystemContext injection
    prior_memory = memory.get_memory(user.id)
    sys_ctx = FamilySystemContext(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        thread_memory=prior_memory,
    )

    try:
        reply_text = await run_agent(prompt, deps=sys_ctx, uid=user.id)

        # Follow-up: if the agent downloaded an image, feed it to the model visually
        if _pending_attachment is not None:
            file_path = _pending_attachment
            _pending_attachment = None
            mime_type, _ = mimetypes.guess_type(file_path)
            if mime_type and mime_type.startswith('image/'):
                try:
                    with open(file_path, "rb") as f:
                        img_data = f.read()
                    follow_up: list = [text] if text else ["Analyze this attachment."]
                    follow_up.append(BinaryContent(data=img_data, media_type=mime_type))
                    reply_text = await run_agent(follow_up, deps=sys_ctx, uid=user.id)
                except Exception as e:
                    logger.error(f"Failed to analyze image attachment: {e}", exc_info=True)

        if update.message:
            await safe_reply(update.message, reply_text)
    except Exception as e:
        logger.error(f"Failed to process request with agent: {e}", exc_info=True)
        if update.message:
            await safe_reply(update.message, "Sorry, an error occurred while processing your request.")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Defense-in-depth: only operate in private 1-on-1 chats to avoid leaking confidential data to groups
    chat = update.effective_chat
    if not chat or (chat.type != constants.ChatType.PRIVATE and chat.type != "private"):
        logger.warning(
            f"Non-private chat {getattr(chat, 'id', 'Unknown')} "
            f"(type: {getattr(chat, 'type', None)}) tried to use /start. Dropping immediately."
        )
        return

    # Strict ID whitelist check - drop unauthorized traffic silently
    if not update.effective_user or update.effective_user.id not in ALLOWED_USER_IDS:
        return

    if update.message:
        await safe_reply(
            update.message,
            "Hello. I am your Family Office Chief of Staff AI Agent. "
            "I can check the family's shared email inbox, track action items, and summarize operations. "
            "How can I assist you?",
        )

async def check_email_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Scheduled email check: starting")
    try:
        emails = await asyncio.to_thread(agent_email.fetch_unread_emails)
        if not emails:
            logger.info("Scheduled email check: no new emails")
            return

        # Check for poison-pill UIDs that repeatedly failed (SEC-05)
        valid_emails = []
        poison_pill_uids = []
        for email_data in emails:
            uid = str(email_data.get("uid", ""))
            fails = _email_failure_counts.get(uid, 0)
            if fails >= MAX_EMAIL_FAILURES:
                try:
                    poison_pill_uids.append(int(uid))
                except (ValueError, TypeError):
                    pass
                logger.error(
                    "Email UID %s exceeded failure threshold (%d attempts). "
                    "Marking as read to prevent poison-pill crash loop.",
                    uid,
                    fails,
                )
            else:
                valid_emails.append(email_data)

        if poison_pill_uids:
            try:
                await asyncio.to_thread(agent_email.mark_emails_as_read, poison_pill_uids)
            except Exception as e:
                logger.error(f"Failed to mark poison-pill emails as read: {e}")
            for p_uid in poison_pill_uids:
                _email_failure_counts.pop(str(p_uid), None)

        if not valid_emails:
            logger.info("Scheduled email check: no valid unread emails remaining after filtering poison pills")
            return

        emails = valid_emails

        # Increment failure count for current run (cleared upon success)
        for email_data in emails:
            uid_str = str(email_data.get("uid", ""))
            _email_failure_counts[uid_str] = _email_failure_counts.get(uid_str, 0) + 1

        email_ids_by_uid: dict[str, int] = {}
        for email_data in emails:
            try:
                eid = email_store.store_email(email_data)
                if eid is not None:
                    email_ids_by_uid[email_data["uid"]] = eid
            except Exception as store_err:
                logger.error("Failed to store email UID %s: %s", email_data.get("uid"), store_err)

        profile = read_profile()
        profile_summary = f"Family Profile:\n{json.dumps(profile, indent=2)}\n"

        out_dir = os.path.join(os.path.dirname(__file__), "downloads")
        os.makedirs(out_dir, exist_ok=True)

        email_summary_lines = []
        image_contents: list = []
        for idx, email_data in enumerate(emails, 1):
            uid = email_data["uid"]
            try:
                summary = (
                    f"[{idx}] UID: {uid}\n"
                    f"From: {email_data.get('sender', '')}\n"
                    f"Subject: {email_data.get('subject', '')}\n"
                    f"Content: {email_data.get('body_snippet', '')}\n"
                )
                if email_data.get('attachments'):
                    for att in email_data['attachments']:
                        att_filename = att.get('filename', 'attachment')
                        att_size = att.get('size_bytes', 0)
                        if att_size > MAX_ATTACHMENT_SIZE_BYTES:
                            logger.warning(
                                "Skipping attachment '%s' in email %s: size %d exceeds limit %d",
                                att_filename,
                                uid,
                                att_size,
                                MAX_ATTACHMENT_SIZE_BYTES,
                            )
                            summary += f"\n[Attachment '{att_filename}' omitted: exceeds size limit]\n"
                            continue

                        if att.get('mime_type') == 'application/pdf':
                            try:
                                cached = email_store.get_downloaded_path(uid, att_filename)
                                if cached and os.path.exists(cached):
                                    if os.path.getsize(cached) <= MAX_ATTACHMENT_SIZE_BYTES:
                                        with open(cached, "rb") as f:
                                            pdf_text = extract_pdf_text(f.read())
                                    else:
                                        pdf_text = ""
                                else:
                                    pdf_bytes = await asyncio.to_thread(fetch_attachment_content_by_uid, uid, att_filename)
                                    if pdf_bytes and len(pdf_bytes) <= MAX_ATTACHMENT_SIZE_BYTES:
                                        pdf_text = extract_pdf_text(pdf_bytes)
                                        safe_filename = os.path.basename(att_filename)
                                        file_path = os.path.join(out_dir, f"{uid}_{safe_filename}")
                                        with open(file_path, "wb") as f:
                                            f.write(pdf_bytes)
                                        eid = email_ids_by_uid.get(uid)
                                        if eid:
                                            email_store.update_attachment_local_path(eid, att_filename, file_path)
                                    else:
                                        pdf_text = ""
                                if pdf_text:
                                    summary += f"\nExtracted text from '{att_filename}':\n{pdf_text}\n"
                            except Exception as e:
                                logger.error(f"Failed to extract PDF from {att_filename} in email {uid}: {e}")
                                summary += f"\n[Attachment '{att_filename}': extraction error]\n"
                        elif att.get('mime_type', '').startswith('image/'):
                            try:
                                cached = email_store.get_downloaded_path(uid, att_filename)
                                if cached and os.path.exists(cached):
                                    if os.path.getsize(cached) <= MAX_ATTACHMENT_SIZE_BYTES:
                                        with open(cached, "rb") as f:
                                            img_data = f.read()
                                    else:
                                        img_data = None
                                else:
                                    img_data = await asyncio.to_thread(fetch_attachment_content_by_uid, uid, att_filename)
                                    if img_data and len(img_data) <= MAX_ATTACHMENT_SIZE_BYTES:
                                        safe_filename = os.path.basename(att_filename)
                                        file_path = os.path.join(out_dir, f"{uid}_{safe_filename}")
                                        with open(file_path, "wb") as f:
                                            f.write(img_data)
                                        eid = email_ids_by_uid.get(uid)
                                        if eid:
                                            email_store.update_attachment_local_path(eid, att_filename, file_path)
                                    else:
                                        img_data = None
                                if img_data:
                                    image_contents.append(BinaryContent(data=img_data, media_type=att['mime_type']))
                            except Exception as e:
                                logger.error(f"Failed to process image {att_filename} in email {uid}: {e}")
                    att_lines = [f"  - {a['filename']} ({a['mime_type']}, {a['size_bytes']} bytes)" for a in email_data['attachments']]
                    summary += "Attachments:\n" + "\n".join(att_lines) + "\n"
                email_summary_lines.append(summary)
            except Exception as single_err:
                logger.error("Error processing email UID %s: %s", uid, single_err, exc_info=True)
                email_summary_lines.append(f"[{idx}] UID: {uid}\n[Error processing email content]\n")

        email_summary = profile_summary + "\n---\n".join(email_summary_lines)

        system_ctx = FamilySystemContext(
            user_id=0,
            username="system",
            first_name="System",
            last_name=None,
            thread_memory=[],
        )

        now_local = datetime.now(LOCAL_TZ)
        off = now_local.strftime("%z")
        local_offset_iso = f"{off[:3]}:{off[3:]}"

        prompt_text = EMAIL_CHECK_TEMPLATE.safe_substitute(
            email_summary=email_summary,
            local_tz=LOCAL_TZ_NAME,
            local_offset=local_offset_iso,
        )

        final_prompt: str | list = [prompt_text] + image_contents
        if len(final_prompt) == 1:
            final_prompt = final_prompt[0]

        reply_text = await run_agent(final_prompt, deps=system_ctx, uid=None, target_agent=email_agent)

        logger.debug("Email check agent response:\n%s", reply_text)

        uid_ints = [int(e['uid']) for e in emails]
        try:
            await asyncio.to_thread(agent_email.mark_emails_as_read, uid_ints)
        except Exception as e:
            logger.error(f"Failed to mark emails as read: {e}")

        # Successfully finished processing these emails, clear failure records
        for email_data in emails:
            _email_failure_counts.pop(str(email_data["uid"]), None)

        stripped = reply_text.strip()
        if stripped.startswith("[URGENT]") or stripped.startswith("[ERROR]"):
            if stripped.startswith("[URGENT]"):
                body = stripped[len("[URGENT]"):].strip()
                message = "⚠️ <b>Urgent update from email check:</b>\n\n" + body
                logger.info("Scheduled email check: urgent items found, notifying users")
            else:
                body = stripped[len("[ERROR]"):].strip()
                message = "❌ <b>Error during email check:</b>\n\n" + body
                logger.info("Scheduled email check: error encountered, notifying users")
            memory.add_message(None, message)
            for uid in ALLOWED_USER_IDS:
                try:
                    await safe_send(context.bot, uid, message)
                except Exception as e:
                    logger.error(f"Failed to send proactive message to user {uid}: {e}")
        else:
            logger.info("Scheduled email check: no urgent items, staying silent")

    except Exception as e:
        logger.error(f"Scheduled email check failed: {e}", exc_info=True)
        for uid in ALLOWED_USER_IDS:
            try:
                await safe_send(context.bot, uid, f"❌ Email check job failed: {e}")
            except Exception as send_err:
                logger.error(f"Failed to send error notification to user {uid}: {send_err}")


async def generate_digest_text(uid: int | None = None) -> str:
    """Build the digest prompt, run the AI agent, and return the briefing text."""
    t0 = datetime.now(timezone.utc)
    logger.info("Digest: generating")
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%A, %Y-%m-%d")

    events = gc.list_all_events(now, now + timedelta(days=30))
    logger.info("Digest: fetched %d calendar events", len(events))
    parsed = []
    for ev in events:
        start_str = _event_start_raw(ev)
        if not start_str:
            continue
        try:
            dt = _parse_iso_for_digest(start_str)
            if dt >= now or ("T" not in start_str and " " not in start_str and dt.date() >= now.astimezone(LOCAL_TZ).date()):
                parsed.append((dt, ev))
        except (ValueError, TypeError):
            continue
    parsed.sort(key=lambda x: x[0])

    next_2 = [ev for dt, ev in parsed if dt <= now + timedelta(days=2)]
    next_7 = [ev for dt, ev in parsed if dt <= now + timedelta(days=7)]
    next_30 = [ev for dt, ev in parsed if dt <= now + timedelta(days=30)]
    logger.info("Digest: %d events in next 2 days, %d in next 7, %d in next 30",
                len(next_2), len(next_7), len(next_30))

    def fmt_cluster(cluster, label):
        if not cluster:
            return f"{label}: None"
        s = "s" if len(cluster) > 1 else ""
        lines = [f"{label}: ({len(cluster)} event{s})"]
        for ev in cluster:
            cal = ev.get("_calendar_summary", "primary")
            raw = _event_start_raw(ev) or "?"
            lines.append(f"- [{cal}] {ev.get('summary', 'Untitled')} at {_format_local_time(raw)}")
        return "\n".join(lines)

    event_section = "\n\n".join([
        fmt_cluster(next_2, "Next 2 Days (Urgent)"),
        fmt_cluster(next_7, "Next 7 Days"),
        fmt_cluster(next_30, "Next 30 Days"),
    ])

    profile = read_profile()

    # Read skipped events (then clear the file)
    skipped_events_str = "None"
    skipped_count = 0
    if os.path.exists(SKIPPED_EVENTS_PATH):
        try:
            with open(SKIPPED_EVENTS_PATH) as f:
                skipped = json.load(f)
            if skipped:
                skipped_count = len(skipped)
                lines = ["Skipped events (already exist on a shared calendar):"]
                for se in skipped:
                    lines.append(f"- {se['title']} on {se['date']} ({se['reason']})")
                skipped_events_str = "\n".join(lines)
            os.remove(SKIPPED_EVENTS_PATH)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Error reading skipped events: %s", e)
            try:
                os.remove(SKIPPED_EVENTS_PATH)
            except OSError:
                pass

    # Gather new emails and events since last digest (read-only, no watermark update)
    last_digest_time = read_last_digest_time()
    new_emails_count = 0
    new_events_count = 0
    if last_digest_time:
        new_emails = email_store.get_emails_since(last_digest_time)
        if new_emails:
            new_emails_count = len(new_emails)
            new_emails_str = "\n".join(
                f"- {e['subject']} (from {e['sender']})" for e in new_emails
            )
        else:
            new_emails_str = "None"

        email_events = event_store.get_recent_events(last_digest_time)
        if email_events:
            new_events_count = len(email_events)
            new_events_lines = []
            for ev in email_events:
                badge = "✅" if ev["synced_to_gcal"] else "📋"
                new_events_lines.append(f"- {badge} {ev['title']} at {_format_local_time(ev['start_iso'])} (ID: {ev['id']})")
            new_events_str = "\n".join(new_events_lines)
        else:
            new_events_str = "None"
    else:
        new_emails_str = "None"
        new_events_str = "None"
    logger.info("Digest: %d new emails, %d new email events, %d skipped events",
                new_emails_count, new_events_count, skipped_count)

    system_ctx = FamilySystemContext(
        user_id=0,
        username="system",
        first_name="System",
        last_name=None,
        thread_memory=[],
    )

    now_local = datetime.now(LOCAL_TZ)
    off = now_local.strftime("%z")
    local_offset_iso = f"{off[:3]}:{off[3:]}"

    bin_data = bin_collection.get_tomorrows_collections()
    logger.info("Digest: bin collections tomorrow: %s", "yes" if bin_data else "no")

    prompt = EVENING_DIGEST_TEMPLATE.safe_substitute(
        today_date=today_str,
        profile=json.dumps(profile, indent=2),
        events=event_section,
        new_emails=new_emails_str,
        new_events_from_emails=new_events_str,
        skipped_events=skipped_events_str,
        bin_collection=bin_data,
        local_tz=LOCAL_TZ_NAME,
        local_offset=local_offset_iso,
    )

    result = await run_agent(prompt, deps=system_ctx, uid=None)

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    logger.info("Digest: done in %.1fs", elapsed)
    logger.debug("Digest: done in %.1fs, response:\n%s", elapsed, result)
    return result


def get_digest_schedule_config(now: datetime | None = None) -> tuple[time, ZoneInfo, str, bool]:
    """Return the configured digest time (with tzinfo), timezone, timezone name, and catch-up flag.

    The evening digest is scheduled in the local UK timezone (configurable via TIMEZONE or
    DIGEST_TIMEZONE, defaulting to 'Europe/London') so that the scheduled wall-clock time
    remains consistent across daylight saving time transitions (GMT/BST).
    """
    digest_time_str = os.getenv("DIGEST_TIME", "19:00")
    digest_tz_name = os.getenv("DIGEST_TIMEZONE", LOCAL_TZ_NAME)
    try:
        digest_tz = ZoneInfo(digest_tz_name)
    except Exception as e:
        logger.warning(
            "Invalid DIGEST_TIMEZONE %r, falling back to %s: %s",
            digest_tz_name,
            LOCAL_TZ_NAME,
            e,
        )
        digest_tz = LOCAL_TZ
        digest_tz_name = LOCAL_TZ_NAME

    hour, minute = map(int, digest_time_str.split(":"))
    digest_time = time(hour, minute, 0, tzinfo=digest_tz)

    send_on_start = os.getenv("SEND_DIGEST_ON_START", str(SEND_DIGEST_ON_START)).lower() == "true"
    should_catch_up = False
    if send_on_start:
        if now is None:
            now_dt = datetime.now(digest_tz)
        elif now.tzinfo is None:
            now_dt = now.replace(tzinfo=digest_tz)
        else:
            now_dt = now.astimezone(digest_tz)
        digest_datetime = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        should_catch_up = (now_dt >= digest_datetime)

    return digest_time, digest_tz, digest_tz_name, should_catch_up


async def evening_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    global DIGEST_RETRY_ATTEMPT
    attempt = DIGEST_RETRY_ATTEMPT
    logger.info("Evening digest: starting (attempt %d/%d)", attempt, MAX_ATTEMPTS)
    try:
        digest_text = await generate_digest_text(uid=None)
    except (ModelHTTPError, httpx.ReadTimeout, httpx.TimeoutException) as e:
        status_code = getattr(e, "status_code", None)
        if (status_code in RETRY_STATUS_CODES or isinstance(e, (httpx.ReadTimeout, httpx.TimeoutException))) and attempt < MAX_ATTEMPTS:
            DIGEST_RETRY_ATTEMPT = attempt + 1
            context.job_queue.run_once(
                evening_digest_job,
                when=RETRY_INTERVAL_MINUTES * 60,
                job_kwargs={"misfire_grace_time": 86400},
            )
            logger.error(
                "Evening digest failed with transient error %s; retrying in %d minutes (attempt %d/%d)",
                type(e).__name__, RETRY_INTERVAL_MINUTES, attempt + 1, MAX_ATTEMPTS,
            )
        else:
            DIGEST_RETRY_ATTEMPT = 1
            logger.error("Evening digest failed: %s", e, exc_info=True)
    except Exception as e:
        if attempt < MAX_ATTEMPTS and ("timeout" in str(e).lower() or "timed out" in str(e).lower() or "503" in str(e)):
            DIGEST_RETRY_ATTEMPT = attempt + 1
            context.job_queue.run_once(
                evening_digest_job,
                when=RETRY_INTERVAL_MINUTES * 60,
                job_kwargs={"misfire_grace_time": 86400},
            )
            logger.error(
                "Evening digest failed with transient error; retrying in %d minutes (attempt %d/%d): %s",
                RETRY_INTERVAL_MINUTES, attempt + 1, MAX_ATTEMPTS, e,
            )
        else:
            DIGEST_RETRY_ATTEMPT = 1
            logger.error(f"Evening digest failed: {e}", exc_info=True)
    else:
        for uid in ALLOWED_USER_IDS:
            try:
                await safe_send(context.bot, uid, digest_text)
            except Exception as e:
                logger.error(f"Failed to send digest to user {uid}: {e}")

        memory.add_message(None, digest_text)
        save_last_digest_time(datetime.now(timezone.utc).isoformat())
        DIGEST_RETRY_ATTEMPT = 1
        logger.info("Evening digest: sent successfully")


async def startup_hello_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a startup hello message to configured users."""
    if not SEND_HI_BYE_USER_IDS:
        logger.info("SEND_HI_BYE is empty, skipping startup hello")
        return
    logger.info("Sending startup hello to configured users")
    text = "👋 <b>Hozbot is back online!</b> Ready to check emails, manage your calendar, and keep things running. <i>Let's go!</i>"
    for uid in SEND_HI_BYE_USER_IDS:
        try:
            await safe_send(context.bot, uid, text)
        except Exception as e:
            logger.error(f"Failed to send startup hello to user {uid}: {e}")


async def shutdown_bye_job(app: Application) -> None:
    """Send a shutdown message to configured users."""
    if not SEND_HI_BYE_USER_IDS:
        logger.info("SEND_HI_BYE is empty, skipping shutdown message")
        return
    logger.info("Sending shutdown message to configured users")
    text = "👋 <b>Hozbot is shutting down.</b> See you next time!"
    for uid in SEND_HI_BYE_USER_IDS:
        try:
            await safe_send(app.bot, uid, text)
        except Exception as e:
            logger.error(f"Failed to send shutdown message to user {uid}: {e}")


def register_handlers(application: Application) -> None:
    """Register command and message handlers with strict private-chat filters."""
    application.add_handler(CommandHandler("start", start_cmd, filters=filters.ChatType.PRIVATE))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & (filters.TEXT | filters.PHOTO | filters.Document.IMAGE | filters.Document.PDF)
            & ~filters.COMMAND,
            handle_message,
        )
    )


def main() -> None:
    configure_logging()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Missing TELEGRAM_BOT_TOKEN environment variable. Exiting.")
        return
    # Ensure storage directory exists
    storage_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "storage")
    os.makedirs(storage_path, exist_ok=True)
    # Initialize profile JSON file if missing
    try:
        read_profile()
    except Exception as e:
        logger.error(f"Error initializing profile file: {e}")
    email_store.init_db()

    # Authenticate Google Calendar (non-interactive — run `uv run google_calendar.py` if needed)
    try:
        auth_ok = gc.try_load_token()
        if not auth_ok:
            logger.warning(
                "Google Calendar not authenticated. Calendar features will be unavailable. "
                "Run `uv run google_calendar.py` to set up OAuth."
            )
        elif os.path.exists(db_mod.CALENDAR_PATH):
            count = migrate_from_json(db_mod.CALENDAR_PATH, gc)
            if count > 0:
                logger.info("Migrated %d events from JSON to Google Calendar", count)
    except Exception as e:
        logger.error(f"Failed to initialize Google Calendar: {e}")

    logger.info("Initializing Family Office Agent Telegram App...")
    application = (
        Application.builder()
        .token(token)
        .defaults(Defaults(
            tzinfo=LOCAL_TZ,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        ))
        .post_stop(shutdown_bye_job)
        .build()
    )

    global _telegram_bot
    _telegram_bot = application.bot

    # Handlers (strictly enforce private 1-on-1 chat to prevent data leakage in groups)
    register_handlers(application)

    # Schedule recurring jobs
    if os.getenv("EMAIL_CHECK_ENABLED", "true").lower() == "true":
        interval = int(os.getenv("EMAIL_CHECK_INTERVAL_MINUTES", "15"))
        application.job_queue.run_repeating(check_email_job, interval=interval * 60, first=10, job_kwargs={"misfire_grace_time": 86400})
        logger.info(f"Scheduled email check every {interval} minutes")

    if os.getenv("DIGEST_ENABLED", "true").lower() == "true":
        digest_time, digest_tz, digest_tz_name, should_catch_up = get_digest_schedule_config()
        digest_time_str = os.getenv("DIGEST_TIME", "19:00")
        application.job_queue.run_daily(
            evening_digest_job, time=digest_time, job_kwargs={"misfire_grace_time": 86400}
        )
        logger.info(f"Scheduled evening digest at {digest_time_str} {digest_tz_name}")

        # Catch up if digest time already passed today (e.g. restart / sleep after digest time)
        if should_catch_up:
            application.job_queue.run_once(
                evening_digest_job, when=5, job_kwargs={"misfire_grace_time": 86400}
            )
            logger.info("Scheduling catch-up evening digest (missed today in %s)", digest_tz_name)

    # Send startup hello
    application.job_queue.run_once(startup_hello_job, when=5)
    logger.info("Scheduled startup hello message")

    logger.info("Starting Telegram long polling bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
