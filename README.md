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
```

This starts a local redirect server and prints an authorization URL. Open it in a **private/incognito** browser window, sign in as the bot account, and authorize the application. The token is saved to `storage/google_calendar_token.json` and refreshes automatically thereafter.

### Remote Server / Docker Setup

When running on a remote server with Docker, use a fixed port (`--port 8080`) and forward it over SSH so your local browser can complete the OAuth redirect:

1. **On the remote server**, run:
   ```bash
   docker run --rm -it --network host --env-file .env -v ./storage:/app/storage mhozza/hozbot python tools/auth_google_calendar.py --port 8080
   ```

2. **On your local machine** (in another terminal), forward port `8080`:
   ```bash
   ssh -N -L 8080:localhost:8080 <user>@<server>
   ```

3. Open the printed URL in your local browser and complete the Google sign-in. The token is saved to `storage/google_calendar_token.json` on the server.

*(Alternatively, run `uv run tools/auth_google_calendar.py` locally and copy `storage/google_calendar_token.json` to the server's `storage/` directory.)*
