import os
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
DEFAULT_EVENT_DURATION_MINUTES = 60


class GoogleCalendar:
    def __init__(self, credentials_path: str, token_path: str, calendar_id: str = "primary"):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.calendar_id = calendar_id
        self.service = None

    def try_load_token(self) -> bool:
        """Try to load and refresh an existing token. Returns True if successful."""
        if not os.path.exists(self.token_path):
            return False
        try:
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
        except (ValueError, json.JSONDecodeError):
            return False

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:
                    return False
            else:
                return False

        self.service = build("calendar", "v3", credentials=creds)
        return True

    def auth_console(self, login_hint: str | None = None) -> None:
        """Run the interactive OAuth console flow to obtain a token.

        Args:
            login_hint: Optional email to pre-fill in the Google OAuth page.
                        Useful when signed into multiple accounts.
        """
        if not os.path.exists(self.credentials_path):
            raise FileNotFoundError(
                f"Google Calendar credentials file not found: {self.credentials_path}. "
                "Download OAuth 2.0 Client ID (Desktop app) JSON from "
                "https://console.cloud.google.com/apis/credentials"
            )

        # Validate credentials file looks like a Desktop app type
        try:
            with open(self.credentials_path) as f:
                cred_data = json.load(f)
            if cred_data.get("installed") is None:
                print(
                    "WARNING: credentials file doesn't look like a 'Desktop app' type.\n"
                    "Make sure you created 'OAuth 2.0 Client ID' with 'Desktop app' type,\n"
                    "not 'Web application' or another type.\n"
                )
        except (json.JSONDecodeError, OSError) as e:
            print(f"WARNING: could not validate credentials file: {e}")

        flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, SCOPES)
        flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"

        auth_kwargs = dict(access_type="offline", include_granted_scopes="true")
        if login_hint:
            auth_kwargs["login_hint"] = login_hint

        url = flow.authorization_url(**auth_kwargs)[0]
        account_hint = f" for {login_hint}" if login_hint else ""
        print(
            "\n" + "=" * 60 + "\n"
            "Google Calendar OAuth — authorize the bot account" + account_hint + "\n"
            "\n"
            "1. COPY the URL below\n"
            "2. Open your browser in PRIVATE / INCOGNITO mode\n"
            "3. Paste the URL and sign in as the bot's Google account\n"
            "\n"
            f"{url}\n"
            + "=" * 60 + "\n"
            "Paste the full authorization code here:\n> ",
            flush=True,
        )

        code = sys.stdin.readline().strip()
        if not code:
            print("No code entered. Aborting.")
            return

        try:
            flow.fetch_token(code=code)
        except Exception as e:
            print(
                f"\nFailed to exchange authorization code: {e}\n\n"
                "Common causes:\n"
                "  1. Your email isn't added as a test user.\n"
                "     Go to: Google Cloud Console → OAuth consent screen → "
                "Audience → Add your email as a test user.\n"
                "  2. The 'calendar' scope isn't added.\n"
                "     Go to: Google Cloud Console → OAuth consent screen → "
                "Scopes → Add '.../auth/calendar'.\n"
                "  3. The credentials file is the wrong type.\n"
                "     Must be 'Desktop app' (look for 'installed' key in the JSON), "
                "not 'Web application'.\n"
                "  4. The authorization code expired (codes expire in ~5 minutes). "
                "Run the tool again.\n"
                "  5. Wrong Google account — use --email flag to specify which account.\n"
            )
            raise

        creds = flow.credentials

        os.makedirs(os.path.dirname(self.token_path) or ".", exist_ok=True)
        with open(self.token_path, "w") as f:
            f.write(creds.to_json())
        logger.info("Google Calendar OAuth token saved to %s", self.token_path)
        self.service = build("calendar", "v3", credentials=creds)

    def _require_service(self):
        if self.service is None:
            raise RuntimeError("GoogleCalendar not authenticated. Call auth_console() first.")

    def list_events(
        self,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        max_results: int = 250,
    ) -> list[dict[str, Any]]:
        self._require_service()
        if time_min is None:
            time_min = datetime.now(timezone.utc)
        if time_max is None:
            time_max = time_min + timedelta(days=90)

        params = {
            "calendarId": self.calendar_id,
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        try:
            events_result = self.service.events().list(**params).execute()
            return events_result.get("items", [])
        except HttpError as e:
            logger.error("Google Calendar API error listing events: %s", e)
            raise

    def create_event(
        self,
        summary: str,
        start_iso: str,
        end_iso: str | None = None,
        description: str | None = None,
        source_email_id: int | None = None,
    ) -> dict[str, Any]:
        self._require_service()

        start_dt = _parse_iso(start_iso)
        if end_iso:
            end_dt = _parse_iso(end_iso)
        else:
            end_dt = start_dt + timedelta(minutes=DEFAULT_EVENT_DURATION_MINUTES)

        body = {
            "summary": summary,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "UTC"},
        }
        if description:
            body["description"] = description

        extended_properties = {}
        if source_email_id is not None:
            extended_properties["private"] = {
                "sourceEmailId": str(source_email_id)
            }
        if extended_properties:
            body["extendedProperties"] = extended_properties

        try:
            event = self.service.events().insert(
                calendarId=self.calendar_id, body=body
            ).execute()
            logger.info("Created Google Calendar event: %s (%s)", event.get("id"), summary)
            return event
        except HttpError as e:
            logger.error("Google Calendar API error creating event: %s", e)
            raise

    def update_event(
        self,
        event_id: str,
        summary: str | None = None,
        start_iso: str | None = None,
        end_iso: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        self._require_service()

        try:
            event = self.service.events().get(
                calendarId=self.calendar_id, eventId=event_id
            ).execute()
        except HttpError as e:
            logger.error("Google Calendar API error fetching event %s: %s", event_id, e)
            raise

        if summary is not None:
            event["summary"] = summary
        if start_iso is not None:
            event["start"] = {"dateTime": _parse_iso(start_iso).isoformat(), "timeZone": "UTC"}
        if end_iso is not None:
            event["end"] = {"dateTime": _parse_iso(end_iso).isoformat(), "timeZone": "UTC"}
        if description is not None:
            event["description"] = description

        try:
            updated = self.service.events().update(
                calendarId=self.calendar_id, eventId=event_id, body=event
            ).execute()
            logger.info("Updated Google Calendar event: %s", event_id)
            return updated
        except HttpError as e:
            logger.error("Google Calendar API error updating event %s: %s", event_id, e)
            raise

    def delete_event(self, event_id: str) -> None:
        self._require_service()
        try:
            self.service.events().delete(
                calendarId=self.calendar_id, eventId=event_id
            ).execute()
            logger.info("Deleted Google Calendar event: %s", event_id)
        except HttpError as e:
            logger.error("Google Calendar API error deleting event %s: %s", event_id, e)
            raise

    def get_event(self, event_id: str) -> dict[str, Any]:
        self._require_service()
        try:
            return self.service.events().get(
                calendarId=self.calendar_id, eventId=event_id
            ).execute()
        except HttpError as e:
            logger.error("Google Calendar API error fetching event %s: %s", event_id, e)
            raise


def _parse_iso(iso_str: str) -> datetime:
    cleaned = iso_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def migrate_from_json(json_path: str, gc: GoogleCalendar) -> int:
    import database
    events = database._read_json(json_path)
    if not events:
        return 0

    migrated = 0
    for ev in events:
        try:
            gc.create_event(
                summary=ev.get("title", "Untitled"),
                start_iso=ev.get("timestamp", ""),
                source_email_id=ev.get("source_email_id"),
            )
            migrated += 1
        except Exception as e:
            logger.error("Failed to migrate event %s: %s", ev.get("id"), e)

    if migrated > 0:
        backup = json_path + ".migrated"
        os.rename(json_path, backup)
        logger.info("Migrated %d events to Google Calendar; backup at %s", migrated, backup)

    return migrated

