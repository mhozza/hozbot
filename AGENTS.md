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
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs (whitelist) |
| `GOOGLE_API_KEY` | Google AI API key (for Gemini) |
| `EMAIL_IMAP_SERVER` | IMAP server (e.g. imap.gmail.com) |
| `EMAIL_ADDRESS` | Email account address |
| `EMAIL_APP_PASSWORD` | Email app password |

## Project Structure
```
hozbot/
├── main.py             # Telegram bot + PydanticAI agent setup + tool definitions
├── agent_email.py      # IMAP email fetching, PDF text extraction (also has CLI entrypoint)
├── database.py         # JSON-backed family profile & calendar storage
├── memory.py           # Per-user thread memory (JSON file)
├── system_prompt.md    # System prompt for the AI agent
├── storage/            # Runtime data (gitignored)
│   ├── family_profile.json
│   ├── calendar_db.json
│   └── thread_memory.json
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
- The agent has access to tools: `check_shared_inbox`, `get_profile`, `add_fact_tool`, `get_calendar`, `add_calendar_event`, `mark_event_sent_tool`, `clear_thread_memory`, `download_attachment`, `extract_pdf_file`
- Thread memory per user persists across messages

## CLI (agent_email.py)
The email module also has a standalone CLI:
```bash
uv run agent_email.py check                          # List unread emails
uv run agent_email.py fetch-attachments <uid>        # Download attachments
uv run agent_email.py extract-pdf <file>             # Extract PDF text
```
