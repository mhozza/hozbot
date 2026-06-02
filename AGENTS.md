# AGENTS.md — hozbot

## Tech Stack
- **Language**: Python 3.13+
- **AI Framework**: `pydantic-ai-slim` (v1.102.0) with Google Gemini models
  - Primary: `gemini-3.5-flash`
  - Fallback: `gemini-3.1-flash-lite`
- **Telegram**: `python-telegram-bot` v21.10
- **Email**: `imapclient` v3.0.1 (IMAP + SSL)
- **PDF**: `pypdf` v5.1.0
- **Config**: `python-dotenv` v1.0.1

## Package Management
- Uses **uv** as the Python package manager
- Lockfile: `uv.lock` (committed to VCS)
- Virtual environment: `.venv/`

## Running Locally
```bash
uv run main.py
```

## Running with Docker
```bash
docker compose up --build -d
```
The Dockerfile uses a multi-stage build with `ghcr.io/astral-sh/uv:python3.13-alpine` for dependency syncing, then a minimal `python:3.13-alpine` runtime stage.

## Environment Variables (`.env`)
| Variable | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs (whitelist) |
| `GOOGLE_API_KEY` | Google AI API key (for Gemini) |
| `EMAIL_IMAP_SERVER` | IMAP server (e.g. imap.gmail.com) |
| `EMAIL_ADDRESS` | Email account address |
| `EMAIL_APP_PASSWORD` | Email app password |
| `EMAIL_CHECK_INTERVAL_MINUTES` | How often to poll the inbox (default: 15) |
| `EMAIL_CHECK_ENABLED` | Enable/disable periodic email checks (default: true) |
| `DIGEST_TIME` | Time for evening digest (default: 19:00) |
| `DIGEST_ENABLED` | Enable/disable evening digest (default: true) |
| `SEND_HI_BYE` | Comma-separated Telegram user IDs for startup/shutdown notifications (default: empty, disabled) |
| `BIN_UPRN` | UPRN for bin collection lookups (see tools/get_uprn.py) |

## Project Structure
```
hozbot/
├── main.py             # Telegram bot + PydanticAI agent setup + tool definitions
├── agent_email.py      # IMAP email fetching, PDF text extraction (also has CLI entrypoint)
├── bin_collection.py   # St Albans bin collection schedule lookup
├── database.py         # JSON-backed family profile & calendar storage
├── email_store.py      # SQLite-backed email storage for digest queries
├── memory.py           # Per-user thread memory (JSON file)
├── prompts/            # AI prompt templates (string.Template format)
│   ├── system_prompt.md
│   ├── email_check.md
│   └── evening_digest.md
├── storage/            # Runtime data (gitignored)
│   ├── family_profile.json
│   ├── calendar_db.json
│   ├── thread_memory.json
│   └── last_digest.json
├── tools/              # Standalone utility scripts
│   └── get_uprn.py     # Resolve address/postcode to UPRN
├── downloads/          # Downloaded email attachments (gitignored)
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
└── uv.lock
```

## Architecture
- Users send messages to a Telegram bot
- The bot validates against `ALLOWED_USER_IDS` (whitelist)
- A `pydantic-ai` Agent receives the message with `FallbackModel` (primary → fallback Gemini model)
- The agent has access to tools: `check_shared_inbox`, `get_profile`, `add_fact_tool`, `get_calendar`, `add_calendar_event`, `mark_event_sent_tool`, `clear_thread_memory`, `download_attachment`, `extract_pdf_file`, `get_current_datetime`, `get_daily_digest`, `check_bin_collection`
- Thread memory per user persists across messages

## Scheduled Jobs (Proactive Behaviour)
The bot uses `python-telegram-bot`'s `JobQueue` for proactive/recurring tasks:

- **Startup notification** (on bot start): Sends a hello message to all users in `SEND_HI_BYE`.
- **Shutdown notification** (on bot stop): Sends a goodbye message to all users in `SEND_HI_BYE`.
- **Email check** (every `EMAIL_CHECK_INTERVAL_MINUTES`): Polls the inbox, feeds new emails to the AI agent for analysis. The agent extracts dates/events and auto-adds relevant ones to the calendar (respecting family profile context). If urgent items (events within 48h) or errors are detected, a proactive message is sent to all authorized users; otherwise it stays silent.
- **Evening digest** (daily at `DIGEST_TIME`): The AI agent compiles a structured briefing covering new emails since last digest, new events auto-created from emails, tomorrow's events, this week, next week, urgent items (relevant to family profile), and an "Other Notable Events" section for events unlikely to be relevant.

Both jobs use a system-level `FamilySystemContext` (`user_id=0`) and can be toggled via env vars.

## CLI (agent_email.py)
The email module also has a standalone CLI:
```bash
uv run agent_email.py check                          # List unread emails
uv run agent_email.py fetch-attachments <uid>        # Download attachments
uv run agent_email.py extract-pdf <file>             # Extract PDF text
```

## CLI (tools/get_uprn.py)
Resolve a postcode or address to a UPRN (for the `BIN_UPRN` env var):
```bash
uv run tools/get_uprn.py "AL1 5TE"
```
