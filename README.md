# hozbot

Personal Telegram AI agent — checks emails, manages Google Calendar, and sends daily digests.

## Quick Start

```bash
cp .env.example .env    # configure tokens and IDs
uv run main.py
```

## Google Calendar OAuth

The bot uses Google Calendar as its event store. Before first use, set it up:

```bash
# Download OAuth 2.0 Client ID (Desktop app) JSON from
# https://console.cloud.google.com/apis/credentials
# Save it to storage/credentials.json

# Run the auth tool:
uv run tools/auth_google_calendar.py --email bot@gmail.com

This prints a URL. Open it in a **private/incognito** browser window, sign in as the bot account, authorize, and paste the code back. The token is saved to `storage/google_calendar_token.json` and refreshes automatically thereafter.
