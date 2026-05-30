import os
import logging
from dataclasses import dataclass
from dotenv import load_dotenv
from string import Template

# Load environment variables first to avoid credential errors during initialization
load_dotenv()
# Retrieve Gemini API key for PydanticAI (used automatically by the provider)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

from pydantic_ai import Agent, RunContext
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import agent_email
from pydantic_ai.capabilities import Thinking
import asyncio
from datetime import datetime, time, timezone, timedelta
from database import read_calendar, read_profile, mark_event_sent
import json
from typing import List
import memory
from dataclasses import field
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")

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

from database import read_profile, append_fact, read_calendar, add_event, mark_event_sent
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
    """Return the list of upcoming calendar events."""
    events = read_calendar()
    if not events:
        return "No events scheduled."
    lines = []
    for ev in events:
        lines.append(f"- {ev.get('title')} at {ev.get('timestamp')} (sent: {ev.get('reminder_sent')})")
    return "\n".join(lines)

@agent.tool
def add_calendar_event(ctx: RunContext[FamilySystemContext], title: str, timestamp_iso: str) -> str:
    """Add a new event to the calendar and return its ID.

    The `ctx` parameter provides execution context as required by the tool schema.
    """
    event = add_event(title, timestamp_iso)
    return f"Event added with ID {event.get('id')}"

@agent.tool
def mark_event_sent_tool(ctx: RunContext[FamilySystemContext], event_id: str) -> str:
    """Mark the calendar event as reminder sent."""
    # Context currently unused but required for tool schema
    mark_event_sent(event_id)
    return f"Event {event_id} marked as reminder sent."

@agent.tool
def clear_thread_memory(ctx: RunContext[FamilySystemContext]) -> str:
    """Clear stored thread memory for the current user."""
    memory.clear_memory(ctx.deps.user_id)
    return "Thread memory cleared."



@agent.tool
def download_attachment(ctx: RunContext[FamilySystemContext], uid: str, filename: str) -> str:
    """Download a specific attachment from an email by UID and filename, save it locally, and return the file path."""
    try:
        data = fetch_attachment_content_by_uid(uid, filename)
        if data is None:
            return f"Attachment '{filename}' not found in email UID {uid}."
        safe_filename = os.path.basename(filename)
        out_dir = os.path.join(os.path.dirname(__file__), "downloads")
        os.makedirs(out_dir, exist_ok=True)
        file_path = os.path.join(out_dir, f"{uid}_{safe_filename}")
        with open(file_path, "wb") as f:
            f.write(data)
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




async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Strict ID whitelist check - drop unauthorized traffic silently
    if not update.effective_user or update.effective_user.id not in ALLOWED_USER_IDS:
        logger.warning(
            f"Unauthorized user {update.effective_user.id if update.effective_user else 'Unknown'} "
            "tried to send a message. Dropping silently."
        )
        return

    user = update.effective_user
    text = update.message.text if update.message else ""
    if not text:
        return

    logger.info(f"Received message from authorized user {user.id} ({user.first_name})")

    # Construct the FamilySystemContext injection
    # Retrieve prior thread memory for this user
    prior_memory = memory.get_memory(user.id)
    sys_ctx = FamilySystemContext(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        thread_memory=prior_memory,
    )

    try:
        # Run the agent asynchronously
        # Run the agent asynchronously
        response = await agent.run(text, deps=sys_ctx)
        
        # Determine reply text (compatible with new AgentRunResult API)
        reply_text = getattr(response, "output", None) or getattr(response, "data", None) or str(response)
        
        # Send reply
        if update.message:
            await update.message.reply_text(reply_text)
        # Store the inbound message and the agent's reply in thread memory
        memory.add_message(user.id, text)
        memory.add_message(user.id, reply_text)
    except Exception as e:
        logger.error(f"Failed to process request with agent: {e}", exc_info=True)
        if update.message:
            await update.message.reply_text("Sorry, an error occurred while processing your request.")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Strict ID whitelist check - drop unauthorized traffic silently
    if not update.effective_user or update.effective_user.id not in ALLOWED_USER_IDS:
        return

    if update.message:
        await update.message.reply_text(
            "Hello. I am your Family Office Chief of Staff AI Agent. "
            "I can check the family's shared email inbox, track action items, and summarize operations. "
            "How can I assist you?"
        )

async def check_email_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Scheduled email check: starting")
    try:
        emails = agent_email.fetch_unread_emails()
        if not emails:
            logger.info("Scheduled email check: no new emails")
            return

        profile = read_profile()
        profile_summary = f"Family Profile:\n{json.dumps(profile, indent=2)}\n"

        email_summary_lines = []
        for idx, email_data in enumerate(emails, 1):
            summary = (
                f"[{idx}] UID: {email_data['uid']}\n"
                f"From: {email_data['sender']}\n"
                f"Subject: {email_data['subject']}\n"
                f"Content: {email_data['body_snippet']}\n"
            )
            if email_data['attachments']:
                for att in email_data['attachments']:
                    if att['mime_type'] == 'application/pdf':
                        try:
                            pdf_bytes = fetch_attachment_content_by_uid(email_data['uid'], att['filename'])
                            if pdf_bytes:
                                pdf_text = extract_pdf_text(pdf_bytes)
                                if pdf_text:
                                    summary += f"\nExtracted text from '{att['filename']}':\n{pdf_text}\n"
                        except Exception as e:
                            logger.error(f"Failed to extract PDF from {att['filename']} in email {email_data['uid']}: {e}")
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

        response = await agent.run(prompt, deps=system_ctx)
        reply_text = getattr(response, "output", None) or getattr(response, "data", None) or str(response)

        logger.info("Email check agent response:\n%s", reply_text)

        uid_ints = [int(e['uid']) for e in emails]
        try:
            agent_email.mark_emails_as_read(uid_ints)
        except Exception as e:
            logger.error(f"Failed to mark emails as read: {e}")

        stripped = reply_text.strip()
        if stripped.startswith("[URGENT]") or stripped.startswith("[ERROR]"):
            if stripped.startswith("[URGENT]"):
                message = "⚠️ *Urgent update from email check:*\n\n" + stripped[len("[URGENT]"):].strip()
                logger.info("Scheduled email check: urgent items found, notifying users")
            else:
                message = "❌ *Error during email check:*\n\n" + stripped[len("[ERROR]"):].strip()
                logger.info("Scheduled email check: error encountered, notifying users")
            for uid in ALLOWED_USER_IDS:
                try:
                    await context.bot.send_message(chat_id=uid, text=message, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Failed to send proactive message to user {uid}: {e}")
        else:
            logger.info("Scheduled email check: no urgent items, staying silent")

    except Exception as e:
        logger.error(f"Scheduled email check failed: {e}", exc_info=True)
        for uid in ALLOWED_USER_IDS:
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"❌ Email check job failed: {str(e)}"
                )
            except Exception as send_err:
                logger.error(f"Failed to send error notification to user {uid}: {send_err}")


async def evening_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Evening digest: starting")
    try:
        now = datetime.now(timezone.utc)
        today_str = now.strftime("%A, %Y-%m-%d")

        # Fetch and cluster calendar events
        events = read_calendar()
        parsed = []
        for ev in events:
            try:
                ts = ev["timestamp"].replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts)
                if dt >= now:
                    parsed.append((dt, ev))
            except (ValueError, KeyError):
                continue
        parsed.sort(key=lambda x: x[0])

        next_2 = [ev for dt, ev in parsed if dt <= now + timedelta(days=2)]
        next_7 = [ev for dt, ev in parsed if dt <= now + timedelta(days=7)]
        next_30 = [ev for dt, ev in parsed if dt <= now + timedelta(days=30)]

        def fmt_cluster(cluster, label):
            if not cluster:
                return f"**{label}**: None"
            s = "s" if len(cluster) > 1 else ""
            lines = [f"**{label}**: ({len(cluster)} event{s})"]
            for ev in cluster:
                lines.append(f"- {ev.get('title')} at {ev.get('timestamp')}")
            return "\n".join(lines)

        event_section = "\n\n".join([
            fmt_cluster(next_2, "Next 2 Days (Urgent)"),
            fmt_cluster(next_7, "Next 7 Days"),
            fmt_cluster(next_30, "Next 30 Days"),
        ])

        profile = read_profile()

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
        )

        response = await agent.run(prompt, deps=system_ctx)
        digest_text = getattr(response, "output", None) or getattr(response, "data", None) or str(response)

        full_message = f"📋 *Evening Family Briefing — {today_str}*\n\n{digest_text}"

        for uid in ALLOWED_USER_IDS:
            try:
                await context.bot.send_message(chat_id=uid, text=full_message, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to send digest to user {uid}: {e}")

        logger.info("Evening digest: sent successfully")
    except Exception as e:
        logger.error(f"Evening digest failed: {e}", exc_info=True)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Missing TELEGRAM_BOT_TOKEN environment variable. Exiting.")
        return
    # Ensure storage directory exists and initialize JSON files
    storage_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), "storage")
    os.makedirs(storage_path, exist_ok=True)
    # Initialize JSON files (they will be created if missing)
    try:
        from database import read_profile, read_calendar
        read_profile()
        read_calendar()
    except Exception as e:
        logger.error(f"Error initializing storage files: {e}")

    logger.info("Initializing Family Office Agent Telegram App...")
    application = Application.builder().token(token).build()

    # Handlers
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

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
        now = datetime.now()
        digest_datetime = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= digest_datetime:
            application.job_queue.run_once(evening_digest_job, when=5, job_kwargs={"misfire_grace_time": 86400})
            logger.info("Scheduling catch-up evening digest (missed today)")

    logger.info("Starting Telegram long polling bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
