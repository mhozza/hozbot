import os
import unittest
from datetime import datetime, time
from unittest.mock import patch
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from telegram.ext import Application, Defaults

from google_calendar import LOCAL_TZ
from main import get_digest_schedule_config


class TestDigestScheduling(unittest.TestCase):
    def test_default_digest_schedule_is_uk_timezone(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DIGEST_TIME", None)
            os.environ.pop("DIGEST_TIMEZONE", None)
            digest_time, digest_tz, digest_tz_name, should_catch_up = get_digest_schedule_config()

        self.assertEqual(digest_time.hour, 19)
        self.assertEqual(digest_time.minute, 0)
        self.assertEqual(digest_time.second, 0)
        self.assertEqual(digest_tz, ZoneInfo("Europe/London"))
        self.assertEqual(digest_tz_name, "Europe/London")
        self.assertEqual(digest_time.tzinfo, ZoneInfo("Europe/London"))

    def test_custom_digest_time(self):
        with patch.dict(os.environ, {"DIGEST_TIME": "20:45"}):
            digest_time, digest_tz, digest_tz_name, _ = get_digest_schedule_config()

        self.assertEqual(digest_time.hour, 20)
        self.assertEqual(digest_time.minute, 45)
        self.assertEqual(digest_time.tzinfo, ZoneInfo("Europe/London"))

    def test_custom_digest_timezone_override(self):
        with patch.dict(os.environ, {"DIGEST_TIMEZONE": "America/New_York"}):
            digest_time, digest_tz, digest_tz_name, _ = get_digest_schedule_config()

        self.assertEqual(digest_tz_name, "America/New_York")
        self.assertEqual(digest_tz, ZoneInfo("America/New_York"))
        self.assertEqual(digest_time.tzinfo, ZoneInfo("America/New_York"))

    def test_invalid_digest_timezone_fallback(self):
        with patch.dict(os.environ, {"DIGEST_TIMEZONE": "Invalid/Timezone"}):
            digest_time, digest_tz, digest_tz_name, _ = get_digest_schedule_config()

        self.assertEqual(digest_tz, LOCAL_TZ)
        self.assertEqual(digest_time.tzinfo, LOCAL_TZ)

    def test_dst_transition_preserves_wall_clock_time(self):
        """Verify that 19:00 Europe/London stays 19:00 across BST and GMT."""
        tz = ZoneInfo("Europe/London")
        t = time(19, 0, 0, tzinfo=tz)
        trigger = CronTrigger(hour=t.hour, minute=t.minute, second=t.second, timezone=t.tzinfo)

        # Summer (BST, UTC+1): June 1, 2026 at 12:00
        summer_noon = datetime(2026, 6, 1, 12, 0, 0, tzinfo=tz)
        next_summer = trigger.get_next_fire_time(None, summer_noon)
        self.assertEqual(next_summer.hour, 19)
        self.assertEqual(next_summer.minute, 0)
        self.assertEqual(next_summer.strftime("%z"), "+0100")

        # Winter (GMT, UTC+0): December 1, 2026 at 12:00
        winter_noon = datetime(2026, 12, 1, 12, 0, 0, tzinfo=tz)
        next_winter = trigger.get_next_fire_time(None, winter_noon)
        self.assertEqual(next_winter.hour, 19)
        self.assertEqual(next_winter.minute, 0)
        self.assertEqual(next_winter.strftime("%z"), "+0000")

    def test_catch_up_flag_when_past_digest_time(self):
        tz = ZoneInfo("Europe/London")
        with patch.dict(os.environ, {"SEND_DIGEST_ON_START": "true", "DIGEST_TIME": "19:00"}):
            # 18:30 (before digest time) -> should NOT catch up
            now_before = datetime(2026, 11, 4, 18, 30, 0, tzinfo=tz)
            _, _, _, catch_up = get_digest_schedule_config(now=now_before)
            self.assertFalse(catch_up)

            # 19:00 (exact digest time) -> should catch up
            now_at = datetime(2026, 11, 4, 19, 0, 0, tzinfo=tz)
            _, _, _, catch_up = get_digest_schedule_config(now=now_at)
            self.assertTrue(catch_up)

            # 19:30 (after digest time) -> should catch up
            now_after = datetime(2026, 11, 4, 19, 30, 0, tzinfo=tz)
            _, _, _, catch_up = get_digest_schedule_config(now=now_after)
            self.assertTrue(catch_up)

    def test_catch_up_flag_disabled_by_default(self):
        tz = ZoneInfo("Europe/London")
        with patch.dict(os.environ, {"SEND_DIGEST_ON_START": "false", "DIGEST_TIME": "19:00"}):
            now_after = datetime(2026, 11, 4, 20, 0, 0, tzinfo=tz)
            _, _, _, catch_up = get_digest_schedule_config(now=now_after)
            self.assertFalse(catch_up)

    def test_application_defaults_tzinfo(self):
        app = (
            Application.builder()
            .token("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
            .defaults(Defaults(tzinfo=LOCAL_TZ))
            .build()
        )
        self.assertEqual(app.bot.defaults.tzinfo, LOCAL_TZ)
        self.assertEqual(app.job_queue.scheduler.timezone, LOCAL_TZ)


if __name__ == "__main__":
    unittest.main()
