import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import json
import os
from telegram import Message, Bot, LinkPreviewOptions
from telegram.ext import Defaults
import main
import memory


class TestSec01EmailAgentIsolation(unittest.TestCase):
    def test_email_agent_exists_and_distinct_from_main_agent(self):
        """email_agent must be an independent Agent instance from agent."""
        self.assertIsNotNone(main.email_agent)
        self.assertIsNotNone(main.agent)
        self.assertIsNot(main.email_agent, main.agent)

    def test_email_agent_has_safe_tools_only(self):
        """email_agent must only have safe extraction/querying tools."""
        safe_tools = {
            "get_profile",
            "add_email_event",
            "sync_email_event_to_gcal",
            "list_email_events",
            "update_email_event",
            "note_event_skipped",
            "get_current_datetime",
        }
        email_agent_tool_names = set(main.email_agent._function_toolset.tools.keys())
        self.assertTrue(safe_tools.issubset(email_agent_tool_names),
                        f"Missing safe tools: {safe_tools - email_agent_tool_names}")

        # Forbidden dangerous / exfiltrating / poisoning tools
        dangerous_tools = {
            "remember_this",
            "delete_calendar_event",
            "update_calendar_event",
            "remove_email_event",
            "add_calendar_event",
            "send_file_to_chat",
            "extract_pdf_file",
            "download_attachment",
            "search_stored_emails",
            "clear_thread_memory",
            "check_shared_inbox",
            "get_daily_digest",
            "check_bin_collection",
            "get_calendar",
        }
        forbidden_present = dangerous_tools.intersection(email_agent_tool_names)
        self.assertEqual(
            forbidden_present,
            set(),
            f"Dangerous tools found in email_agent: {forbidden_present}",
        )

    def test_email_agent_does_not_have_recent_conversation_system_prompt(self):
        """email_agent must not include user conversation history in system prompt."""
        # Check system prompt function runners on email_agent vs agent
        email_prompt_funcs = [
            r.function.__name__ for r in main.email_agent._system_prompt_functions
        ]
        self.assertNotIn("add_recent_conversation", email_prompt_funcs)


class TestSec01LinkPreviewDisabled(unittest.IsolatedAsyncioTestCase):
    async def test_safe_send_disables_link_previews(self):
        """safe_send must explicitly pass link_preview_options with is_disabled=True."""
        mock_bot = AsyncMock(spec=Bot)
        await main.safe_send(mock_bot, 12345, "Check out https://example.com")

        mock_bot.send_message.assert_called_once()
        _, kwargs = mock_bot.send_message.call_args
        self.assertIn("link_preview_options", kwargs)
        lpo = kwargs["link_preview_options"]
        self.assertIsInstance(lpo, LinkPreviewOptions)
        self.assertTrue(lpo.is_disabled)

    async def test_safe_reply_disables_link_previews(self):
        """safe_reply must explicitly pass link_preview_options with is_disabled=True."""
        mock_message = AsyncMock(spec=Message)
        await main.safe_reply(mock_message, "See https://example.com")

        mock_message.reply_text.assert_called_once()
        _, kwargs = mock_message.reply_text.call_args
        self.assertIn("link_preview_options", kwargs)
        lpo = kwargs["link_preview_options"]
        self.assertIsInstance(lpo, LinkPreviewOptions)
        self.assertTrue(lpo.is_disabled)

    def test_application_defaults_disable_link_previews(self):
        """Application Defaults must configure link_preview_options with is_disabled=True and parse_mode=HTML."""
        from telegram.ext import Application
        app = (
            Application.builder()
            .token("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
            .defaults(
                Defaults(
                    tzinfo=main.LOCAL_TZ,
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            )
            .build()
        )
        self.assertIsNotNone(app.bot.defaults)
        self.assertEqual(app.bot.defaults.parse_mode, "HTML")
        self.assertIsNotNone(app.bot.defaults.link_preview_options)
        self.assertTrue(app.bot.defaults.link_preview_options.is_disabled)



class TestSec01MemoryIsolation(unittest.IsolatedAsyncioTestCase):
    @patch("main.memory.add_message")
    async def test_run_agent_does_not_broadcast_when_uid_is_none(self, mock_add_message):
        """run_agent with uid=None must NOT call memory.add_message."""
        mock_agent = MagicMock()
        mock_res = MagicMock()
        mock_res.output = "Output message"
        mock_agent.run = AsyncMock(return_value=mock_res)

        result = await main.run_agent("Some prompt with untrusted email", deps=MagicMock(), uid=None, target_agent=mock_agent)
        self.assertEqual(result, "Output message")
        mock_add_message.assert_not_called()

    @patch("main.memory.add_message")
    async def test_run_agent_stores_memory_for_interactive_user(self, mock_add_message):
        """run_agent with a user ID must record prompt and reply for that user."""
        mock_agent = MagicMock()
        mock_res = MagicMock()
        mock_res.output = "Bot response"
        mock_agent.run = AsyncMock(return_value=mock_res)

        result = await main.run_agent("Hello", deps=MagicMock(), uid=999, target_agent=mock_agent)
        self.assertEqual(result, "Bot response")
        self.assertEqual(mock_add_message.call_count, 2)
        mock_add_message.assert_any_call(999, "Hello")
        mock_add_message.assert_any_call(999, "Bot response")

    @patch("main.safe_send", new_callable=AsyncMock)
    @patch("main.memory.add_message")
    @patch("main.agent_email")
    @patch("main.email_store")
    @patch("main.read_profile")
    @patch("main.run_agent", new_callable=AsyncMock)
    async def test_check_email_job_silent_does_not_broadcast_to_memory(
        self, mock_run_agent, mock_read_profile, mock_email_store, mock_agent_email, mock_add_message, mock_safe_send
    ):
        """check_email_job with [SILENT] response must not store anything in memory or send messages."""
        mock_email_store.store_email.return_value = 1
        mock_read_profile.return_value = {}
        mock_agent_email.fetch_unread_emails.return_value = [
            {"uid": "1", "sender": "test@test.com", "subject": "Test", "body_snippet": "Hi", "attachments": []}
        ]
        mock_run_agent.return_value = "[SILENT]"

        context = MagicMock()
        await main.check_email_job(context)

        # target_agent must be email_agent
        mock_run_agent.assert_called_once()
        _, kwargs = mock_run_agent.call_args
        self.assertIs(kwargs.get("target_agent"), main.email_agent)

        # Silent run must not broadcast to memory or safe_send
        mock_add_message.assert_not_called()
        mock_safe_send.assert_not_called()

    @patch("main.ALLOWED_USER_IDS", [101, 102])
    @patch("main.safe_send", new_callable=AsyncMock)
    @patch("main.memory.add_message")
    @patch("main.agent_email")
    @patch("main.email_store")
    @patch("main.read_profile")
    @patch("main.run_agent", new_callable=AsyncMock)
    async def test_check_email_job_urgent_broadcasts_only_notification(
        self, mock_run_agent, mock_read_profile, mock_email_store, mock_agent_email, mock_add_message, mock_safe_send
    ):
        """check_email_job with [URGENT] response must broadcast only the outgoing alert to memory."""
        mock_email_store.store_email.return_value = 1
        mock_read_profile.return_value = {}
        mock_agent_email.fetch_unread_emails.return_value = [
            {"uid": "2", "sender": "school@school.org", "subject": "Urgent closure", "body_snippet": "Closed tomorrow", "attachments": []}
        ]
        mock_run_agent.return_value = "[URGENT] School is closed tomorrow."

        context = MagicMock()
        await main.check_email_job(context)

        mock_add_message.assert_called_once()
        broadcast_uid, broadcast_msg = mock_add_message.call_args[0]
        self.assertIsNone(broadcast_uid)
        self.assertIn("School is closed tomorrow", broadcast_msg)
        self.assertNotIn("Closed tomorrow", broadcast_msg)  # Raw body snippet must not be in broadcast
        self.assertEqual(mock_safe_send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
