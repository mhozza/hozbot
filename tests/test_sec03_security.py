import logging
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import main
from main import (
    configure_logging,
    is_debug_mode,
    check_email_job,
    generate_digest_text,
)


class LogCaptureHandler(logging.Handler):
    """Simple in-memory log handler to inspect emitted LogRecord objects."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord):
        self.records.append(record)

    def clear(self):
        self.records.clear()

    @property
    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]

    def records_for_level(self, level: int) -> list[logging.LogRecord]:
        return [r for r in self.records if r.levelno == level]


class TestSec03LoggingConfiguration(unittest.TestCase):
    def setUp(self):
        self.orig_env = os.environ.copy()
        # Clean any preexisting debug/environment overrides
        os.environ.pop("DEBUG", None)
        os.environ.pop("ENVIRONMENT", None)
        os.environ.pop("ENV", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)
        # Restore release configuration after tests
        configure_logging(debug=False)

    def test_default_is_release_mode(self):
        """Without env vars or arguments, configuration must default to release mode."""
        self.assertFalse(is_debug_mode())
        is_debug = configure_logging()
        self.assertFalse(is_debug)

        # httpx and httpcore loggers must be WARNING to suppress plaintext tokens
        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertEqual(logging.getLogger("httpcore").level, logging.WARNING)
        self.assertEqual(logging.getLogger().level, logging.INFO)
        self.assertEqual(logging.getLogger("main").level, logging.INFO)

    def test_explicit_release_mode(self):
        """Explicit release arguments configure WARNING for httpx/httpcore and INFO for root."""
        for env_arg in ["production", "prod", "release", "PRODUCTION", "Release"]:
            is_debug = configure_logging(environment=env_arg)
            self.assertFalse(is_debug, f"Expected release mode for environment={env_arg}")
            self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)
            self.assertEqual(logging.getLogger("httpcore").level, logging.WARNING)
            self.assertEqual(logging.getLogger().level, logging.INFO)

        for debug_arg in [False, "0", "false", "False", "no", "off"]:
            is_debug = configure_logging(debug=debug_arg)
            self.assertFalse(is_debug, f"Expected release mode for debug={debug_arg}")
            self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)
            self.assertEqual(logging.getLogger("httpcore").level, logging.WARNING)

    def test_explicit_development_mode(self):
        """Explicit development arguments configure DEBUG for httpx/httpcore and root."""
        for env_arg in ["development", "dev", "debug", "test", "testing", "DEVELOPMENT"]:
            is_debug = configure_logging(environment=env_arg)
            self.assertTrue(is_debug, f"Expected debug mode for environment={env_arg}")
            self.assertEqual(logging.getLogger("httpx").level, logging.DEBUG)
            self.assertEqual(logging.getLogger("httpcore").level, logging.DEBUG)
            self.assertEqual(logging.getLogger().level, logging.DEBUG)

        for debug_arg in [True, "1", "true", "True", "yes", "on", "dev"]:
            is_debug = configure_logging(debug=debug_arg)
            self.assertTrue(is_debug, f"Expected debug mode for debug={debug_arg}")
            self.assertEqual(logging.getLogger("httpx").level, logging.DEBUG)
            self.assertEqual(logging.getLogger("httpcore").level, logging.DEBUG)
            self.assertEqual(logging.getLogger().level, logging.DEBUG)

    def test_env_var_debug_mode(self):
        """Environment variables DEBUG and ENVIRONMENT configure the logging modes."""
        # DEBUG=1
        os.environ["DEBUG"] = "1"
        self.assertTrue(is_debug_mode())
        self.assertTrue(configure_logging())
        self.assertEqual(logging.getLogger("httpx").level, logging.DEBUG)

        # DEBUG=0
        os.environ["DEBUG"] = "0"
        self.assertFalse(is_debug_mode())
        self.assertFalse(configure_logging())
        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)

        # ENVIRONMENT=development
        os.environ.pop("DEBUG", None)
        os.environ["ENVIRONMENT"] = "development"
        self.assertTrue(is_debug_mode())
        self.assertTrue(configure_logging())
        self.assertEqual(logging.getLogger("httpx").level, logging.DEBUG)

        # ENVIRONMENT=production
        os.environ["ENVIRONMENT"] = "production"
        self.assertFalse(is_debug_mode())
        self.assertFalse(configure_logging())
        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)

    def test_explicit_argument_precedence(self):
        """Explicit function arguments take precedence over environment variables."""
        # Environment says development, but caller explicitly requests release
        os.environ["ENVIRONMENT"] = "development"
        os.environ["DEBUG"] = "1"
        is_debug = configure_logging(debug=False)
        self.assertFalse(is_debug)
        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)

        # Environment says production, but caller explicitly requests debug
        os.environ["ENVIRONMENT"] = "production"
        os.environ["DEBUG"] = "0"
        is_debug = configure_logging(debug=True)
        self.assertTrue(is_debug)
        self.assertEqual(logging.getLogger("httpx").level, logging.DEBUG)

    def test_httpx_token_leakage_suppressed_in_release(self):
        """In release mode, httpx INFO request logs containing bot tokens are dropped."""
        handler = LogCaptureHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            # 1. Release mode: suppress httpx INFO logs
            configure_logging(debug=False)
            httpx_logger = logging.getLogger("httpx")
            token_message = 'HTTP Request: POST https://api.telegram.org/bot987654321:SECRET_TOKEN_HERE/getUpdates "HTTP/1.1 200 OK"'

            httpx_logger.info(token_message)
            self.assertEqual(
                len(handler.records),
                0,
                "httpx INFO request log was not suppressed in release mode",
            )
            self.assertNotIn("SECRET_TOKEN_HERE", "\n".join(handler.messages))

            # WARNING level logs from httpx should still pass
            httpx_logger.warning("Connection pool approaching saturation")
            self.assertEqual(len(handler.records), 1)
            self.assertIn("Connection pool approaching saturation", handler.messages[0])

            # 2. Development mode: httpx INFO request logs are retained
            handler.clear()
            configure_logging(debug=True)
            httpx_logger.info(token_message)
            self.assertEqual(
                len(handler.records),
                1,
                "httpx INFO request log was not captured in development mode",
            )
            self.assertIn("SECRET_TOKEN_HERE", handler.messages[0])
        finally:
            root.removeHandler(handler)

    def test_httpcore_info_suppressed_in_release(self):
        """In release mode, httpcore INFO request logs are dropped."""
        handler = LogCaptureHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            configure_logging(debug=False)
            httpcore_logger = logging.getLogger("httpcore")
            httpcore_logger.info("connect_tcp.started host='api.telegram.org' port=443")
            self.assertEqual(len(handler.records), 0)

            # In development mode, httpcore logs are retained
            configure_logging(debug=True)
            httpcore_logger.info("connect_tcp.complete return_value=<Connection>")
            self.assertEqual(len(handler.records), 1)
        finally:
            root.removeHandler(handler)


class TestSec03LLMResponseLogging(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.handler = LogCaptureHandler()
        self.app_logger = logging.getLogger("main")
        self.app_logger.addHandler(self.handler)
        self.orig_env = os.environ.copy()
        os.environ.pop("DEBUG", None)
        os.environ.pop("ENVIRONMENT", None)

    def tearDown(self):
        self.app_logger.removeHandler(self.handler)
        os.environ.clear()
        os.environ.update(self.orig_env)
        configure_logging(debug=False)

    async def test_check_email_job_downgrades_llm_response_to_debug(self):
        """check_email_job must log the full agent response at DEBUG, not INFO."""
        secret_email_response = "SECRET_EMAIL_AGENT_RESPONSE: Dr. John appointment on Friday 3pm"

        mock_email = {
            "uid": "1001",
            "sender": "clinic@example.com",
            "subject": "Appointment Confirmation",
            "body_snippet": "Your appointment is confirmed for Friday 3pm.",
            "attachments": [],
        }

        # 1. In release mode: response text must NOT appear in INFO logs
        configure_logging(debug=False)
        self.handler.clear()

        with patch("agent_email.fetch_unread_emails", return_value=[mock_email]), \
             patch("email_store.store_email", return_value=1), \
             patch("main.read_profile", return_value={}), \
             patch("main.run_agent", new_callable=AsyncMock) as mock_run_agent, \
             patch("agent_email.mark_emails_as_read") as mock_mark:
            mock_run_agent.return_value = secret_email_response

            mock_context = MagicMock()
            await check_email_job(mock_context)

        # In release mode (level INFO), logger.debug messages are discarded by the logger
        # Verify no captured record contains the secret response
        for record in self.handler.records:
            self.assertNotIn(
                secret_email_response,
                record.getMessage(),
                "Secret email response text found in release log output",
            )
        info_records = self.handler.records_for_level(logging.INFO)
        self.assertFalse(
            any(secret_email_response in r.getMessage() for r in info_records),
            "Secret email response text found in INFO log records",
        )

        # 2. In debug/development mode: response text IS captured at DEBUG
        configure_logging(debug=True)
        self.handler.clear()

        with patch("agent_email.fetch_unread_emails", return_value=[mock_email]), \
             patch("email_store.store_email", return_value=1), \
             patch("main.read_profile", return_value={}), \
             patch("main.run_agent", new_callable=AsyncMock) as mock_run_agent, \
             patch("agent_email.mark_emails_as_read") as mock_mark:
            mock_run_agent.return_value = secret_email_response

            mock_context = MagicMock()
            await check_email_job(mock_context)

        debug_records = [
            r for r in self.handler.records
            if secret_email_response in r.getMessage()
        ]
        self.assertTrue(
            len(debug_records) > 0,
            "Expected secret email response to be logged in debug mode",
        )
        for r in debug_records:
            self.assertEqual(
                r.levelno,
                logging.DEBUG,
                f"Expected LLM response to be logged at DEBUG level, got {r.levelname}",
            )

    async def test_generate_digest_text_downgrades_llm_response_to_debug(self):
        """generate_digest_text must log full response at DEBUG, but retain elapsed at INFO."""
        secret_digest_response = "SECRET_DIGEST_RESULT: Family Schedule: Tom at soccer 10am, Kate dentist 2pm"

        # 1. In release mode: response text must NOT appear in INFO logs, but elapsed time should
        configure_logging(debug=False)
        self.handler.clear()

        with patch("main.gc.list_all_events", return_value=[]), \
             patch("main.read_profile", return_value={}), \
             patch("main.read_last_digest_time", return_value=None), \
             patch("bin_collection.get_tomorrows_collections", return_value=""), \
             patch("main.run_agent", new_callable=AsyncMock) as mock_run_agent:
            mock_run_agent.return_value = secret_digest_response

            result = await generate_digest_text(uid=None)
            self.assertEqual(result, secret_digest_response)

        # Ensure no INFO log contains the secret response
        info_records = self.handler.records_for_level(logging.INFO)
        info_messages = [r.getMessage() for r in info_records]

        for msg in info_messages:
            self.assertNotIn(
                secret_digest_response,
                msg,
                "Full LLM digest response found in INFO log record",
            )

        # Ensure operational elapsed time is still reported at INFO
        elapsed_logged = any("Digest: done in " in msg for msg in info_messages)
        self.assertTrue(
            elapsed_logged,
            "Expected 'Digest: done in ' operational timing log at INFO level",
        )

        # 2. In debug/development mode: response text IS captured at DEBUG
        configure_logging(debug=True)
        self.handler.clear()

        with patch("main.gc.list_all_events", return_value=[]), \
             patch("main.read_profile", return_value={}), \
             patch("main.read_last_digest_time", return_value=None), \
             patch("bin_collection.get_tomorrows_collections", return_value=""), \
             patch("main.run_agent", new_callable=AsyncMock) as mock_run_agent:
            mock_run_agent.return_value = secret_digest_response

            result = await generate_digest_text(uid=None)
            self.assertEqual(result, secret_digest_response)

        debug_records = [
            r for r in self.handler.records
            if secret_digest_response in r.getMessage()
        ]
        self.assertTrue(
            len(debug_records) > 0,
            "Expected digest response to be logged at DEBUG in development mode",
        )
        for r in debug_records:
            self.assertEqual(
                r.levelno,
                logging.DEBUG,
                f"Expected LLM response log to have DEBUG level, got {r.levelname}",
            )


if __name__ == "__main__":
    unittest.main()
