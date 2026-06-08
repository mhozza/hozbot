import os
import logging
from dataclasses import dataclass
from dotenv import load_dotenv
from string import Template
# Load environment variables first to avoid credential errors during initialization
load_dotenv()
# Retrieve Gemini API key for PydanticAI (used automatically by the provider)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

from pydantic_ai import Agent, RunContext, BinaryContent
from telegram import Update, Message
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram_utils import sanitize_telegram_html
import agent_email
import email_store
from pydantic_ai.capabilities import Thinking
import asyncio
from datetime import datetime, time, timezone, timedelta
from database import read_profile, append_fact
import json
from typing import List
import memory
from dataclasses import field
import bin_collection
# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
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

LAST_DIGEST_PATH = os.path.join(BASE_DIR, "storage", "last_digest.json")


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
    """Send a reply with HTML parse mode, sanitizing unsupported tags."""
    return await message.reply_text(sanitize_telegram_html(text), parse_mode="HTML")


async def safe_send(bot, chat_id: int, text: str) -> Message:
    """Send a proactive message with HTML parse mode, sanitizing unsupported tags."""
    return await bot.send_message(chat_id=chat_id, text=sanitize_telegram_html(text), parse_mode="HTML")


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
from google_calendar import GoogleCalendar, migrate_from_json, _parse_iso
import database as db_mod
import event_store

# Initialize Google Calendar client
GOOGLE_CALENDAR_CREDS = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_FILE", os.path.join(BASE_DIR, "storage", "credentials.json"))
GOOGLE_CALENDAR_TOKEN = os.getenv("GOOGLE_CALENDAR_TOKEN_FILE", os.path.join(BASE_DIR, "storage", "google_calendar_token.json"))
gc = GoogleCalendar(GOOGLE_CALENDAR_CREDS, GOOGLE_CALENDAR_TOKEN)


def _parse_iso_for_digest(iso_str: str) -> datetime:
    return _parse_iso(iso_str)




# Initialize Google provider with API key from environment
google_provider = GoogleProvider(api_key=os.getenv("GOOGLE_API_KEY"))
# Initialize GoogleModel instances for primary and fallback
primary_model = GoogleModel('gemini-3.5-flash', provider=google_provider)
fallback_model = GoogleModel('gemini-3.1-flash-lite', provider=google_provider)
# Use FallbackModel to try primary first, then fallback
model = FallbackModel(primary_model, fallback_model)

# Load prompt templates
SYSTEM_PROMPT = open(os.path.join(PROMPTS_DIR, "system_prompt.md")).read().strip()
EMAIL_CHECK_TEMPLATE = Template(open(os.path.join(PROMPTS_DIR, "email_check.md")).read().strip())
EVENING_DIGEST_TEMPLATE = Template(open(os.path.join(PROMPTS_DIR, "evening_digest.md")).read().strip())

# Initialize PydanticAI Agent with GoogleModel instance
agent = Agent(
    model,
    deps_type=FamilySystemContext,
    capabilities=[Thinking(effort='low')],
    system_prompt=SYSTEM_PROMPT,
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

from agent_email import extract_pdf_text, fetch_attachment_content_by_uid

@agent.tool
def get_profile(ctx: RunContext[FamilySystemContext]) -> str:
    """Return the family profile JSON as a formatted string."""
    profile = read_profile()
    return json.dumps(profile, indent=2)

@agent.tool
def add_fact_tool(ctx: RunContext[FamilySystemContext], note: str) -> str:
    """Add a critical note to the family profile.

    The `ctx` parameter provides execution context and is required by the tool schema.
    """
    # Context is currently unused but retained for compliance with tool schema requirements.
    append_fact(note)
    return "Fact added to profile."

@agent.tool
def get_calendar(ctx: RunContext[FamilySystemContext]) -> str:
    """Return the list of upcoming calendar events (next 90 days)."""
    try:
        now = datetime.now(timezone.utc)
        events = gc.list_events(now, now + timedelta(days=90))
        if not events:
            return "No events scheduled."
        lines = ["Upcoming events (next 90 days):"]
        for ev in events:
            title = ev.get("summary", "Untitled")
            start = ev.get("start", {}).get("dateTime", "?")
            event_id = ev.get("id", "?")
            lines.append(f"- {title} at {start} (ID: {event_id})")
        return "\n".join(lines)
    except Exception as e:
        logger.error("Error in get_calendar tool: %s", e, exc_info=True)
        return f"Error retrieving calendar: {str(e)}"

@agent.tool
def add_calendar_event(ctx: RunContext[FamilySystemContext], title: str, start_iso: str, end_iso: str | None = None) -> str:
    """Add a new event directly to the Google Calendar.

    - title: Event title/summary
    - start_iso: ISO 8601 start datetime (e.g. "2026-06-23T09:00:00Z")
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
def add_email_event(ctx: RunContext[FamilySystemContext], title: str, start_iso: str, end_iso: str | None = None, email_uid: str | None = None, sync_to_gcal: bool = False) -> str:
    """Store an event extracted from an email in the local database.

    Use this for ALL dates found in emails. If the event is relevant to the family
    based on the profile, set sync_to_gcal=True to also publish it to Google Calendar.

    - title: Event title/summary
    - start_iso: ISO 8601 start datetime (e.g. "2026-06-23T09:00:00Z")
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
def list_email_events(ctx: RunContext[FamilySystemContext], date: str | None = None, title: str | None = None) -> str:
    """List upcoming email-extracted events from the local database.
    Optionally filter by date (YYYY-MM-DD) and/or title (substring match, case-insensitive).
    Shows sync status: ✅ = synced to Google Calendar, 📋 = local only.
    """
    try:
        events = event_store.get_future_events(days=90)
        if date:
            events = [ev for ev in events if ev["start_iso"].startswith(date)]
        if title:
            title_lower = title.lower()
            events = [ev for ev in events if title_lower in ev["title"].lower()]
        if not events:
            return "No email-extracted events matching your criteria."
        lines = ["Email-extracted events:"]
        for ev in events:
            badge = "✅" if ev["synced_to_gcal"] else "📋"
            lines.append(f"- {badge} {ev['title']} at {ev['start_iso']} (ID: {ev['id']})")
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
def get_current_datetime() -> str:
    """Return the current date and time in the local timezone."""
    now = datetime.now().astimezone()
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


@agent.tool
def download_attachment(ctx: RunContext[FamilySystemContext], uid: str, filename: str) -> str:
    """Download a specific attachment from an email by UID and filename, save it locally, and return the file path.
    
    Checks the local cache first to avoid re-downloading from IMAP.
    """
    try:
        cached = email_store.get_downloaded_path(uid, filename)
        if cached:
            return f"Already downloaded at {cached} ({os.path.getsize(cached)} bytes)"

        data = fetch_attachment_content_by_uid(uid, filename)
        if data is None:
            return f"Attachment '{filename}' not found in email UID {uid}."
        safe_filename = os.path.basename(filename)
        out_dir = os.path.join(os.path.dirname(__file__), "downloads")
        os.makedirs(out_dir, exist_ok=True)
        file_path = os.path.join(out_dir, f"{uid}_{safe_filename}")
        with open(file_path, "wb") as f:
            f.write(data)

        email_data = email_store.get_email_by_uid(uid)
        if email_data:
            email_store.update_attachment_local_path(email_data["id"], filename, file_path)

        return f"Saved to {file_path} ({len(data)} bytes)"
    except Exception as e:
        logger.error(f"Error downloading attachment: {e}", exc_info=True)
        return f"Error downloading attachment: {str(e)}"

@agent.tool
def extract_pdf_file(ctx: RunContext[FamilySystemContext], file_path: str) -> str:
    """Extract all text from a PDF file at the given path."""
    try:
        with open(file_path, "rb") as f:
            pdf_bytes = f.read()
        text = extract_pdf_text(pdf_bytes)
        if not text:
            return "No text could be extracted from the PDF."
        return text
    except Exception as e:
        logger.error(f"Error extracting PDF text: {e}", exc_info=True)
        return f"Error extracting PDF text: {str(e)}"

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


async def run_agent(prompt, deps, uid: int | None):
    """Run agent.run(), log prompt + response to memory, return reply text."""
    response = await agent.run(prompt, deps=deps)
    reply_text = getattr(response, "output", None) or getattr(response, "data", None) or str(response)

    if isinstance(prompt, str):
        prompt_text = prompt
    else:
        prompt_text = " ".join(str(p) for p in prompt if isinstance(p, str)) or "[Media]"
    memory.add_message(uid, prompt_text)
    memory.add_message(uid, reply_text)

    return reply_text


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            file = await context.bot.get_file(photo.file_id)
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
                file = await context.bot.get_file(doc.file_id)
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

        if update.message:
            await safe_reply(update.message, reply_text)
    except Exception as e:
        logger.error(f"Failed to process request with agent: {e}", exc_info=True)
        if update.message:
            await safe_reply(update.message, "Sorry, an error occurred while processing your request.")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        emails = agent_email.fetch_unread_emails()
        if not emails:
            logger.info("Scheduled email check: no new emails")
            return

        email_ids_by_uid: dict[str, int] = {}
        for email_data in emails:
            eid = email_store.store_email(email_data)
            if eid is not None:
                email_ids_by_uid[email_data["uid"]] = eid

        profile = read_profile()
        profile_summary = f"Family Profile:\n{json.dumps(profile, indent=2)}\n"

        out_dir = os.path.join(os.path.dirname(__file__), "downloads")
        os.makedirs(out_dir, exist_ok=True)

        email_summary_lines = []
        for idx, email_data in enumerate(emails, 1):
            uid = email_data["uid"]
            summary = (
                f"[{idx}] UID: {uid}\n"
                f"From: {email_data['sender']}\n"
                f"Subject: {email_data['subject']}\n"
                f"Content: {email_data['body_snippet']}\n"
            )
            if email_data['attachments']:
                for att in email_data['attachments']:
                    if att['mime_type'] == 'application/pdf':
                        try:
                            cached = email_store.get_downloaded_path(uid, att['filename'])
                            if cached:
                                with open(cached, "rb") as f:
                                    pdf_text = extract_pdf_text(f.read())
                            else:
                                pdf_bytes = fetch_attachment_content_by_uid(uid, att['filename'])
                                if pdf_bytes:
                                    pdf_text = extract_pdf_text(pdf_bytes)
                                    safe_filename = os.path.basename(att['filename'])
                                    file_path = os.path.join(out_dir, f"{uid}_{safe_filename}")
                                    with open(file_path, "wb") as f:
                                        f.write(pdf_bytes)
                                    eid = email_ids_by_uid.get(uid)
                                    if eid:
                                        email_store.update_attachment_local_path(eid, att['filename'], file_path)
                                else:
                                    pdf_text = ""
                            if pdf_text:
                                summary += f"\nExtracted text from '{att['filename']}':\n{pdf_text}\n"
                        except Exception as e:
                            logger.error(f"Failed to extract PDF from {att['filename']} in email {uid}: {e}")
                att_lines = [f"  - {a['filename']} ({a['mime_type']}, {a['size_bytes']} bytes)" for a in email_data['attachments']]
                summary += "Attachments:\n" + "\n".join(att_lines) + "\n"
            email_summary_lines.append(summary)
        email_summary = profile_summary + "\n---\n".join(email_summary_lines)

        system_ctx = FamilySystemContext(
            user_id=0,
            username="system",
            first_name="System",
            last_name=None,
            thread_memory=[],
        )

        prompt = EMAIL_CHECK_TEMPLATE.safe_substitute(email_summary=email_summary)

        reply_text = await run_agent(prompt, deps=system_ctx, uid=None)

        logger.info("Email check agent response:\n%s", reply_text)

        uid_ints = [int(e['uid']) for e in emails]
        try:
            agent_email.mark_emails_as_read(uid_ints)
        except Exception as e:
            logger.error(f"Failed to mark emails as read: {e}")

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
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%A, %Y-%m-%d")

    events = gc.list_events(now, now + timedelta(days=30))
    parsed = []
    for ev in events:
        start_str = ev.get("start", {}).get("dateTime")
        if not start_str:
            continue
        try:
            dt = _parse_iso_for_digest(start_str)
            if dt >= now:
                parsed.append((dt, ev))
        except (ValueError, TypeError):
            continue
    parsed.sort(key=lambda x: x[0])

    next_2 = [ev for dt, ev in parsed if dt <= now + timedelta(days=2)]
    next_7 = [ev for dt, ev in parsed if dt <= now + timedelta(days=7)]
    next_30 = [ev for dt, ev in parsed if dt <= now + timedelta(days=30)]

    def fmt_cluster(cluster, label):
        if not cluster:
            return f"{label}: None"
        s = "s" if len(cluster) > 1 else ""
        lines = [f"{label}: ({len(cluster)} event{s})"]
        for ev in cluster:
            lines.append(f"- {ev.get('summary', 'Untitled')} at {ev.get('start', {}).get('dateTime', '?')}")
        return "\n".join(lines)

    event_section = "\n\n".join([
        fmt_cluster(next_2, "Next 2 Days (Urgent)"),
        fmt_cluster(next_7, "Next 7 Days"),
        fmt_cluster(next_30, "Next 30 Days"),
    ])

    profile = read_profile()

    # Gather new emails and events since last digest (read-only, no watermark update)
    last_digest_time = read_last_digest_time()
    if last_digest_time:
        new_emails = email_store.get_emails_since(last_digest_time)
        if new_emails:
            new_emails_str = "\n".join(
                f"- {e['subject']} (from {e['sender']})" for e in new_emails
            )
        else:
            new_emails_str = "None"

        email_events = event_store.get_recent_events(last_digest_time)
        if email_events:
            new_events_lines = []
            for ev in email_events:
                badge = "✅" if ev["synced_to_gcal"] else "📋"
                new_events_lines.append(f"- {badge} {ev['title']} at {ev['start_iso']} (ID: {ev['id']})")
            new_events_str = "\n".join(new_events_lines)
        else:
            new_events_str = "None"
    else:
        new_emails_str = "None"
        new_events_str = "None"

    system_ctx = FamilySystemContext(
        user_id=0,
        username="system",
        first_name="System",
        last_name=None,
        thread_memory=[],
    )

    prompt = EVENING_DIGEST_TEMPLATE.safe_substitute(
        today_date=today_str,
        profile=json.dumps(profile, indent=2),
        events=event_section,
        new_emails=new_emails_str,
        new_events_from_emails=new_events_str,
        bin_collection=bin_collection.check_schedule(),
    )

    return await run_agent(prompt, deps=system_ctx, uid=uid)


async def evening_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Evening digest: starting")
    try:
        digest_text = await generate_digest_text(uid=None)

        for uid in ALLOWED_USER_IDS:
            try:
                await safe_send(context.bot, uid, digest_text)
            except Exception as e:
                logger.error(f"Failed to send digest to user {uid}: {e}")

        save_last_digest_time(datetime.now(timezone.utc).isoformat())
        logger.info("Evening digest: sent successfully")
    except Exception as e:
        logger.error(f"Evening digest failed: {e}", exc_info=True)


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
            await app.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to send shutdown message to user {uid}: {e}")


def main() -> None:
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
    application = Application.builder().token(token).post_stop(shutdown_bye_job).build()

    # Handlers
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.Document.IMAGE | filters.Document.PDF) & ~filters.COMMAND, handle_message))

    # Schedule recurring jobs
    if os.getenv("EMAIL_CHECK_ENABLED", "true").lower() == "true":
        interval = int(os.getenv("EMAIL_CHECK_INTERVAL_MINUTES", "15"))
        application.job_queue.run_repeating(check_email_job, interval=interval * 60, first=10, job_kwargs={"misfire_grace_time": 86400})
        logger.info(f"Scheduled email check every {interval} minutes")

    if os.getenv("DIGEST_ENABLED", "true").lower() == "true":
        digest_time_str = os.getenv("DIGEST_TIME", "19:00")
        hour, minute = map(int, digest_time_str.split(":"))
        digest_time = time(hour, minute, 0)
        application.job_queue.run_daily(evening_digest_job, time=digest_time, job_kwargs={"misfire_grace_time": 86400})
        logger.info(f"Scheduled evening digest at {digest_time_str}")

        # Catch up if digest time already passed today (e.g. restart / sleep after digest time)
        if SEND_DIGEST_ON_START:
            now = datetime.now()
            digest_datetime = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now >= digest_datetime:
                application.job_queue.run_once(evening_digest_job, when=5, job_kwargs={"misfire_grace_time": 86400})
                logger.info("Scheduling catch-up evening digest (missed today)")

    # Send startup hello
    application.job_queue.run_once(startup_hello_job, when=5)
    logger.info("Scheduled startup hello message")

    logger.info("Starting Telegram long polling bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
