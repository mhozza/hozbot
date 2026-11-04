import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from google_calendar import GoogleCalendar, _event_start_raw
from main import _format_local_time


class TestCalendarEvents(unittest.TestCase):
    def test_event_start_raw_timed_event(self):
        ev = {"start": {"dateTime": "2026-11-04T10:00:00Z"}}
        self.assertEqual(_event_start_raw(ev), "2026-11-04T10:00:00Z")

    def test_event_start_raw_allday_event(self):
        ev = {"start": {"date": "2026-11-04"}}
        self.assertEqual(_event_start_raw(ev), "2026-11-04")

    def test_event_start_raw_empty_and_edge_cases(self):
        self.assertEqual(_event_start_raw({}), "")
        self.assertEqual(_event_start_raw({"start": {}}), "")
        self.assertEqual(_event_start_raw({"start": None}), "")
        self.assertEqual(_event_start_raw({"start": {"dateTime": None, "date": None}}), "")

    def test_list_all_events_sorting(self):
        gc = GoogleCalendar("dummy_creds", "dummy_token")
        gc.service = MagicMock()
        gc.list_calendars = MagicMock(return_value=[
            {"id": "cal1", "summary": "Calendar 1"},
            {"id": "cal2", "summary": "Calendar 2"},
        ])

        # Calendar 1 has an all-day event on Nov 5 and a timed event on Nov 3
        cal1_events = [
            {"id": "allday-nov5", "summary": "Trip", "start": {"date": "2026-11-05"}},
            {"id": "timed-nov3", "summary": "Doctor", "start": {"dateTime": "2026-11-03T15:00:00Z"}},
        ]
        # Calendar 2 has a timed event on Nov 4 and an all-day event on Nov 4
        cal2_events = [
            {"id": "timed-nov4", "summary": "Meeting", "start": {"dateTime": "2026-11-04T10:00:00Z"}},
            {"id": "allday-nov4", "summary": "Birthday", "start": {"date": "2026-11-04"}},
        ]

        def mock_list_events(**kwargs):
            if kwargs.get("calendar_id") == "cal1":
                return [dict(e) for e in cal1_events]
            return [dict(e) for e in cal2_events]

        gc.list_events = MagicMock(side_effect=mock_list_events)

        events = gc.list_all_events()
        event_ids = [e["id"] for e in events]

        # Chronological order: Nov 3 timed, Nov 4 all-day, Nov 4 timed, Nov 5 all-day
        self.assertEqual(event_ids, ["timed-nov3", "allday-nov4", "timed-nov4", "allday-nov5"])

    def test_format_local_time_date_only(self):
        self.assertEqual(_format_local_time("2026-11-04"), "Wed 04 Nov")

    def test_format_local_time_datetime(self):
        # 10:00 UTC in Europe/London in November (GMT) is 10:00
        self.assertEqual(_format_local_time("2026-11-04T10:00:00Z"), "Wed 04 Nov, 10:00")

    def test_format_local_time_question_mark_and_invalid(self):
        self.assertEqual(_format_local_time("?"), "?")
        self.assertEqual(_format_local_time(""), "")
        self.assertEqual(_format_local_time("invalid-date"), "invalid-date")

    @patch("main.gc")
    def test_get_calendar_all_day_and_timed(self, mock_gc):
        from main import get_calendar
        mock_gc.list_all_events.return_value = [
            {"id": "ev1", "summary": "Alice Birthday", "_calendar_summary": "Birthdays", "start": {"date": "2026-11-04"}},
            {"id": "ev2", "summary": "Dentist", "_calendar_summary": "Personal", "start": {"dateTime": "2026-11-04T14:00:00Z"}},
        ]
        result = get_calendar(MagicMock())
        expected = (
            "Upcoming events (next 90 days):\n"
            "- [Birthdays] Alice Birthday at Wed 04 Nov (ID: ev1)\n"
            "- [Personal] Dentist at Wed 04 Nov, 14:00 (ID: ev2)"
        )
        self.assertEqual(result, expected)

    @patch("main.gc")
    @patch("main.event_store")
    def test_list_email_events_matching_allday(self, mock_event_store, mock_gc):
        from main import list_email_events
        mock_event_store.find_events_by_title_date.return_value = []
        mock_gc.list_all_events.return_value = [
            {"id": "ev1", "summary": "Inset Day", "_calendar_summary": "School", "start": {"date": "2026-11-04"}},
        ]
        result = list_email_events(MagicMock(), date="2026-11-04", check_gcal=True)
        self.assertIn("Matching events on shared calendars:", result)
        self.assertIn("- [School] Inset Day at 2026-11-04", result)

    @patch("main.gc")
    @patch("main.read_profile")
    @patch("main.email_store")
    @patch("main.event_store")
    @patch("main.run_agent")
    def test_generate_digest_includes_allday_events(self, mock_run_agent, mock_event_store, mock_email_store, mock_profile, mock_gc):
        import asyncio
        from main import generate_digest_text

        mock_profile.return_value = {}
        mock_email_store.get_emails_since.return_value = []
        mock_event_store.get_recent_events.return_value = []
        mock_run_agent.return_value = "Briefing text"

        # Today's all-day event
        today_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mock_gc.list_all_events.return_value = [
            {"id": "allday-today", "summary": "School Inset", "_calendar_summary": "School", "start": {"date": today_date}},
        ]

        asyncio.run(generate_digest_text())
        mock_run_agent.assert_called_once()
        prompt_passed = mock_run_agent.call_args[0][0]
        self.assertIn("School Inset", prompt_passed)



if __name__ == "__main__":
    unittest.main()
