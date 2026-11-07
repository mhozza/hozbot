"""Unit tests verifying SEC-08 security remediation:
- Restriction of Google Calendar OAuth scopes to calendar.readonly and calendar.events
- Migration from deprecated OOB (urn:ietf:wg:oauth:2.0:oob) to flow.run_local_server()
- Persistence of refreshed tokens back to disk with restricted permissions (0600)
- Preservation of calendar operations without regression
"""

import json
import os
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import google_calendar
from google_calendar import GoogleCalendar, SCOPES
from tools import auth_google_calendar


def _get_mode(path: str) -> int:
    """Return POSIX permission bits (e.g. 0o700, 0o600)."""
    return stat.S_IMODE(os.stat(path).st_mode)


class TestGoogleCalendarOAuthScopes(unittest.TestCase):
    """Verify Google Calendar OAuth scopes are strictly minimized."""

    def test_scopes_are_restricted(self):
        """Scopes must be limited to calendar.readonly and calendar.events."""
        expected = [
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
        ]
        self.assertEqual(SCOPES, expected)

    def test_full_calendar_admin_scope_excluded(self):
        """Full calendar admin scope must NOT be present."""
        full_scope = "https://www.googleapis.com/auth/calendar"
        self.assertNotIn(full_scope, SCOPES)

    def test_try_load_token_uses_restricted_scopes(self):
        """try_load_token must pass restricted SCOPES to Credentials.from_authorized_user_file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = os.path.join(tmpdir, "token.json")
            with open(token_path, "w") as f:
                json.dump({"token": "valid_token"}, f)

            gc = GoogleCalendar("dummy_creds.json", token_path)

            mock_creds = MagicMock()
            mock_creds.valid = True

            with patch("google_calendar.Credentials.from_authorized_user_file", return_value=mock_creds) as mock_from_file, \
                 patch("google_calendar.build") as mock_build:
                success = gc.try_load_token()

            self.assertTrue(success)
            mock_from_file.assert_called_once_with(token_path, SCOPES)
            self.assertEqual(mock_from_file.call_args[0][1], [
                "https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/calendar.events",
            ])

    def test_auth_console_uses_restricted_scopes(self):
        """auth_console must pass restricted SCOPES to InstalledAppFlow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            creds_path = os.path.join(tmpdir, "credentials.json")
            token_path = os.path.join(tmpdir, "token.json")
            with open(creds_path, "w") as f:
                json.dump({"installed": {"client_id": "cid", "client_secret": "csec"}}, f)

            gc = GoogleCalendar(creds_path, token_path)

            mock_flow = MagicMock()
            mock_creds = MagicMock()
            mock_creds.to_json.return_value = json.dumps({"token": "tok"})
            mock_flow.run_local_server.return_value = mock_creds

            with patch("google_calendar.InstalledAppFlow.from_client_secrets_file", return_value=mock_flow) as mock_from_file, \
                 patch("google_calendar.build"):
                gc.auth_console()

            mock_from_file.assert_called_once_with(creds_path, SCOPES)
            self.assertEqual(os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE"), "1")


class TestGoogleCalendarAuthLocalServerFlow(unittest.TestCase):
    """Verify migration from deprecated OOB to flow.run_local_server()."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.creds_path = os.path.join(self.temp_dir.name, "credentials.json")
        self.token_path = os.path.join(self.temp_dir.name, "google_calendar_token.json")
        with open(self.creds_path, "w") as f:
            json.dump({"installed": {"client_id": "test_id", "client_secret": "test_sec"}}, f)

        self.gc = GoogleCalendar(self.creds_path, self.token_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_auth_console_uses_run_local_server_and_not_oob(self):
        """auth_console must use flow.run_local_server and must not set OOB redirect_uri or prompt for manual code."""
        mock_flow = MagicMock()
        mock_creds = MagicMock()
        mock_creds.to_json.return_value = json.dumps({"token": "oauth_token_val"})
        mock_flow.run_local_server.return_value = mock_creds

        with patch("google_calendar.InstalledAppFlow.from_client_secrets_file", return_value=mock_flow), \
             patch("google_calendar.build") as mock_build, \
             patch("sys.stdin.readline") as mock_readline:

            self.gc.auth_console()

        mock_flow.run_local_server.assert_called_once()
        # Ensure OOB redirect URI is NOT configured
        self.assertNotEqual(getattr(mock_flow, "redirect_uri", None), "urn:ietf:wg:oauth:2.0:oob")
        # Ensure manual code entry from stdin is NOT used
        mock_readline.assert_not_called()
        self.assertIsNotNone(self.gc.service)

    def test_auth_console_default_arguments(self):
        """Default arguments for auth_console must be port=0 and open_browser=False."""
        mock_flow = MagicMock()
        mock_creds = MagicMock()
        mock_creds.to_json.return_value = json.dumps({"token": "oauth_token_val"})
        mock_flow.run_local_server.return_value = mock_creds

        with patch("google_calendar.InstalledAppFlow.from_client_secrets_file", return_value=mock_flow), \
             patch("google_calendar.build"):
            self.gc.auth_console()

        call_kwargs = mock_flow.run_local_server.call_args[1]
        self.assertEqual(call_kwargs.get("port"), 0)
        self.assertIs(call_kwargs.get("open_browser"), False)
        self.assertEqual(call_kwargs.get("access_type"), "offline")
        self.assertEqual(call_kwargs.get("include_granted_scopes"), "true")
        self.assertIn("authorization_prompt_message", call_kwargs)
        self.assertIn("{url}", call_kwargs["authorization_prompt_message"])

    def test_auth_console_custom_port_and_browser_flag(self):
        """auth_console must pass customized port, open_browser, and login_hint."""
        mock_flow = MagicMock()
        mock_creds = MagicMock()
        mock_creds.to_json.return_value = json.dumps({"token": "oauth_token_val"})
        mock_flow.run_local_server.return_value = mock_creds

        with patch("google_calendar.InstalledAppFlow.from_client_secrets_file", return_value=mock_flow), \
             patch("google_calendar.build"):
            self.gc.auth_console(login_hint="bot@example.com", port=8080, open_browser=True)

        call_kwargs = mock_flow.run_local_server.call_args[1]
        self.assertEqual(call_kwargs.get("port"), 8080)
        self.assertIs(call_kwargs.get("open_browser"), True)
        self.assertEqual(call_kwargs.get("login_hint"), "bot@example.com")
        self.assertEqual(call_kwargs.get("access_type"), "offline")

    def test_auth_console_missing_credentials_raises_file_not_found(self):
        """Missing credentials file must raise FileNotFoundError with actionable instructions."""
        bad_gc = GoogleCalendar("/nonexistent/credentials.json", self.token_path)
        with self.assertRaises(FileNotFoundError) as ctx:
            bad_gc.auth_console()
        self.assertIn("Google Calendar credentials file not found", str(ctx.exception))

    def test_auth_console_warns_on_non_desktop_credentials(self):
        """Non-desktop credentials JSON must print a warning."""
        with open(self.creds_path, "w") as f:
            json.dump({"web": {"client_id": "web_id"}}, f)

        mock_flow = MagicMock()
        mock_creds = MagicMock()
        mock_creds.to_json.return_value = json.dumps({"token": "tok"})
        mock_flow.run_local_server.return_value = mock_creds

        with patch("google_calendar.InstalledAppFlow.from_client_secrets_file", return_value=mock_flow), \
             patch("google_calendar.build"), \
             patch("builtins.print") as mock_print:
            self.gc.auth_console()

        warning_printed = any("Desktop app" in str(call) for call in mock_print.call_args_list)
        self.assertTrue(warning_printed)

    def test_auth_console_handles_run_local_server_error(self):
        """If run_local_server raises, auth_console must log and re-raise with error instructions."""
        mock_flow = MagicMock()
        mock_flow.run_local_server.side_effect = RuntimeError("Server port bind failed")

        with patch("google_calendar.InstalledAppFlow.from_client_secrets_file", return_value=mock_flow), \
             patch("builtins.print") as mock_print:
            with self.assertRaises(RuntimeError):
                self.gc.auth_console()

        guidance_printed = any("calendar.readonly" in str(call) for call in mock_print.call_args_list)
        self.assertTrue(guidance_printed)


class TestTokenPersistenceAndPermissions(unittest.TestCase):
    """Verify token saving and persistence of refreshed tokens with 0600 permissions."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.token_path = os.path.join(self.temp_dir.name, "storage", "google_calendar_token.json")
        self.creds_path = os.path.join(self.temp_dir.name, "storage", "credentials.json")
        self.gc = GoogleCalendar(self.creds_path, self.token_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_token_creates_0600_file_and_0700_dir(self):
        """_save_token must write the token file with 0600 permissions and directory with 0700."""
        mock_creds = MagicMock()
        mock_creds.to_json.return_value = json.dumps({
            "token": "test_access_token",
            "refresh_token": "test_refresh_token",
            "scopes": SCOPES,
        })

        self.gc._save_token(mock_creds)

        token_dir = os.path.dirname(self.token_path)
        self.assertTrue(os.path.isdir(token_dir))
        self.assertEqual(_get_mode(token_dir), 0o700)
        self.assertTrue(os.path.isfile(self.token_path))
        self.assertEqual(_get_mode(self.token_path), 0o600)

        with open(self.token_path) as f:
            data = json.load(f)
        self.assertEqual(data["token"], "test_access_token")
        self.assertEqual(data["scopes"], SCOPES)

    def test_try_load_token_persists_refreshed_token_with_0600(self):
        """When token is expired and refresh_token is present, creds.refresh() must persist with 0600 mode."""
        os.makedirs(os.path.dirname(self.token_path), mode=0o700, exist_ok=True)
        with open(self.token_path, "w") as f:
            json.dump({"token": "old_token"}, f)
        os.chmod(self.token_path, 0o644)

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "valid_refresh_token"
        mock_creds.to_json.return_value = json.dumps({
            "token": "newly_refreshed_access_token",
            "refresh_token": "valid_refresh_token",
            "scopes": SCOPES,
        })

        with patch("google_calendar.Credentials.from_authorized_user_file", return_value=mock_creds), \
             patch("google_calendar.build") as mock_build:
            success = self.gc.try_load_token()

        self.assertTrue(success)
        mock_creds.refresh.assert_called_once()
        self.assertEqual(_get_mode(self.token_path), 0o600)

        with open(self.token_path) as f:
            saved_data = json.load(f)
        self.assertEqual(saved_data["token"], "newly_refreshed_access_token")

    def test_try_load_token_enforces_0600_on_valid_token(self):
        """If valid token already exists on disk with 0644, try_load_token must enforce 0600."""
        os.makedirs(os.path.dirname(self.token_path), mode=0o700, exist_ok=True)
        with open(self.token_path, "w") as f:
            json.dump({"token": "current_valid_token"}, f)
        os.chmod(self.token_path, 0o644)
        self.assertEqual(_get_mode(self.token_path), 0o644)

        mock_creds = MagicMock()
        mock_creds.valid = True

        with patch("google_calendar.Credentials.from_authorized_user_file", return_value=mock_creds), \
             patch("google_calendar.build"):
            success = self.gc.try_load_token()

        self.assertTrue(success)
        self.assertEqual(_get_mode(self.token_path), 0o600)

    def test_try_load_token_handles_refresh_error(self):
        """When creds.refresh raises an exception, try_load_token must return False gracefully."""
        os.makedirs(os.path.dirname(self.token_path), mode=0o700, exist_ok=True)
        with open(self.token_path, "w") as f:
            json.dump({"token": "old_token"}, f)

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "valid_refresh_token"
        mock_creds.refresh.side_effect = Exception("Google token endpoint unavailable")

        with patch("google_calendar.Credentials.from_authorized_user_file", return_value=mock_creds):
            success = self.gc.try_load_token()

        self.assertFalse(success)
        self.assertIsNone(self.gc.service)

    def test_try_load_token_missing_file_returns_false(self):
        """Missing token file must return False without crashing."""
        self.assertFalse(self.gc.try_load_token())

    def test_try_load_token_invalid_json_returns_false(self):
        """Corrupted/invalid token JSON must return False."""
        os.makedirs(os.path.dirname(self.token_path), mode=0o700, exist_ok=True)
        with open(self.token_path, "w") as f:
            f.write("not valid json content {{{")

        self.assertFalse(self.gc.try_load_token())


class TestAuthGoogleCalendarCli(unittest.TestCase):
    """Verify tools/auth_google_calendar.py CLI and authenticate function."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.creds_path = os.path.join(self.temp_dir.name, "credentials.json")
        self.token_path = os.path.join(self.temp_dir.name, "token.json")
        with open(self.creds_path, "w") as f:
            json.dump({"installed": {"client_id": "cid"}}, f)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_authenticate_invokes_google_calendar_auth_console(self):
        """authenticate() must instantiate GoogleCalendar and call auth_console with arguments."""
        with patch("google_calendar.GoogleCalendar.auth_console") as mock_auth_console:
            gc = auth_google_calendar.authenticate(
                credentials_path=self.creds_path,
                token_path=self.token_path,
                login_hint="user@gmail.com",
                port=8085,
                open_browser=True,
            )

            self.assertEqual(gc.credentials_path, self.creds_path)
            self.assertEqual(gc.token_path, self.token_path)
            mock_auth_console.assert_called_once_with(
                login_hint="user@gmail.com",
                port=8085,
                open_browser=True,
            )

    def test_cli_main_default_arguments(self):
        """main([]) must invoke authenticate with default port=0 and open_browser=False."""
        with patch("tools.auth_google_calendar.authenticate") as mock_auth, \
             patch.dict(os.environ, {
                 "GOOGLE_CALENDAR_CREDENTIALS_FILE": self.creds_path,
                 "GOOGLE_CALENDAR_TOKEN_FILE": self.token_path,
             }), \
             patch("database.CALENDAR_PATH", os.path.join(self.temp_dir.name, "nonexistent.json")):

            mock_auth.return_value = MagicMock()
            auth_google_calendar.main([])

            mock_auth.assert_called_once_with(
                self.creds_path,
                self.token_path,
                login_hint=None,
                port=0,
                open_browser=False,
            )

    def test_cli_main_with_flags(self):
        """main() with --email, --port, and --open-browser must pass flags appropriately."""
        with patch("tools.auth_google_calendar.authenticate") as mock_auth, \
             patch.dict(os.environ, {
                 "GOOGLE_CALENDAR_CREDENTIALS_FILE": self.creds_path,
                 "GOOGLE_CALENDAR_TOKEN_FILE": self.token_path,
             }), \
             patch("database.CALENDAR_PATH", os.path.join(self.temp_dir.name, "nonexistent.json")):

            mock_auth.return_value = MagicMock()
            auth_google_calendar.main(["--email", "test@test.org", "--port", "9090", "--open-browser"])

            mock_auth.assert_called_once_with(
                self.creds_path,
                self.token_path,
                login_hint="test@test.org",
                port=9090,
                open_browser=True,
            )


class TestGoogleCalendarOperationsWithRestrictedScopes(unittest.TestCase):
    """Verify GoogleCalendar operations continue to function with the restricted scopes."""

    def setUp(self):
        self.gc = GoogleCalendar("dummy_creds.json", "dummy_token.json")
        self.mock_service = MagicMock()
        self.gc.service = self.mock_service

    def test_list_calendars(self):
        """list_calendars executes calendarList().list()."""
        self.mock_service.calendarList().list().execute.return_value = {
            "items": [{"id": "primary", "summary": "Main"}]
        }
        calendars = self.gc.list_calendars()
        self.assertEqual(len(calendars), 1)
        self.assertEqual(calendars[0]["summary"], "Main")

    def test_list_events(self):
        """list_events executes events().list()."""
        self.mock_service.events().list().execute.return_value = {
            "items": [{"id": "ev1", "summary": "Meeting"}]
        }
        events = self.gc.list_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["summary"], "Meeting")

    def test_create_event(self):
        """create_event executes events().insert()."""
        self.mock_service.events().insert().execute.return_value = {
            "id": "new_ev_123",
            "summary": "Doctor",
        }
        created = self.gc.create_event(summary="Doctor", start_iso="2026-11-10T10:00:00Z")
        self.assertEqual(created["id"], "new_ev_123")

    def test_delete_event(self):
        """delete_event executes events().delete()."""
        self.mock_service.events().delete().execute.return_value = {}
        self.gc.delete_event("ev_to_delete")
        self.mock_service.events().delete().execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
