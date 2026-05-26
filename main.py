import os
import logging
from dataclasses import dataclass
from dotenv import load_dotenv

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
from datetime import datetime
from database import read_calendar, mark_event_sent
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

# Initialize Google provider with API key from environment
google_provider = GoogleProvider(api_key=os.getenv("GOOGLE_API_KEY"))
# Create GoogleModel instance for Gemini 3.5 flash
google_model = GoogleModel('gemini-3.5-flash', provider=google_provider)

# Initialize PydanticAI Agent with GoogleModel instance
agent = Agent(
    google_model,
    deps_type=FamilySystemContext,
    capabilities=[Thinking(effort='low')],
    system_prompt=open(os.path.join(os.path.dirname(__file__), "system_prompt.md")).read().strip()
)

@agent.tool
def check_shared_inbox(ctx: RunContext[FamilySystemContext]) -> str:
    """Fetch and summarize unread emails in the shared family office inbox.
    
    Returns a detailed summary of unread emails, including sender, subject, and snippet/body.
    """
    try:
        emails = agent_email.fetch_unread_emails()
        if not emails:
            return "No unread emails found."
        
        email_summaries = []
        for idx, email_data in enumerate(emails, 1):
            email_summaries.append(
                f"[{idx}] From: {email_data['sender']}\n"
                f"Subject: {email_data['subject']}\n"
                f"Content: {email_data['body_snippet']}\n"
            )
        return "\n".join(email_summaries)
    except Exception as e:
        logger.error(f"Error in check_shared_inbox tool: {e}", exc_info=True)
        return f"Error retrieving emails: {str(e)}"

from database import read_profile, write_profile, append_fact, read_calendar, add_event, mark_event_sent

@agent.tool
def get_profile(ctx: RunContext[FamilySystemContext]) -> str:
    """Return the family profile JSON as a formatted string."""
    profile = read_profile()
    return json.dumps(profile, indent=2)

@agent.tool
def add_fact_tool(note: str) -> str:
    """Add a critical note to the family profile."""
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
def add_calendar_event(title: str, timestamp_iso: str) -> str:
    """Add a new event to the calendar and return its ID."""
    event = add_event(title, timestamp_iso)
    return f"Event added with ID {event.get('id')}"

@agent.tool
def mark_event_sent_tool(event_id: str) -> str:
    """Mark the calendar event as reminder sent."""
    mark_event_sent(event_id)
    return f"Event {event_id} marked as reminder sent."

@agent.tool
def clear_thread_memory(ctx: RunContext[FamilySystemContext]) -> str:
    """Clear stored thread memory for the current user."""
    memory.clear_memory(ctx.deps.user_id)
    return "Thread memory cleared."



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

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Missing TELEGRAM_BOT_TOKEN environment variable. Exiting.")
        return

    logger.info("Initializing Family Office Agent Telegram App...")
    
    # Initialize python-telegram-bot application with run_polling
    application = Application.builder().token(token).build()

    # Handlers
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Starting Telegram long polling bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
