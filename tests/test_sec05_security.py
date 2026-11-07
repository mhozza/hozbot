import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import os
import io
import pypdf
from telegram import Update, Message, Chat, User, Document, PhotoSize, File, constants
from telegram.ext import ContextTypes
import main
import agent_email
from main import DOWNLOADS_DIR, FamilySystemContext


class TestSec05PyPdfUpgrade(unittest.TestCase):
    def test_pypdf_version_is_safe(self):
        """pypdf version must be >= 5.6.0 to fix known DoS/decompression vulnerabilities."""
        version_parts = []
        for part in pypdf.__version__.split("."):
            try:
                version_parts.append(int(part))
            except ValueError:
                break
        self.assertGreaterEqual(
            tuple(version_parts[:2]),
            (5, 6),
            f"pypdf version {pypdf.__version__} is older than required 5.6.0+",
        )

    def test_pyproject_toml_has_safe_pypdf_dependency(self):
        """pyproject.toml must specify pypdf version >= 5.6.0."""
        pyproject_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pyproject.toml")
        with open(pyproject_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertTrue(
            ("pypdf>=5.6.0" in content) or ("pypdf==" in content and int(pypdf.__version__.split(".")[0]) >= 5),
            "pyproject.toml does not contain safe pypdf version requirement",
        )


class TestSec05ImapClientTimeout(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {
            "EMAIL_IMAP_SERVER": "imap.example.com",
            "EMAIL_ADDRESS": "family@example.com",
            "EMAIL_APP_PASSWORD": "secret_password",
        })
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    @patch("agent_email.IMAPClient")
    def test_fetch_unread_emails_uses_default_timeout(self, mock_imap_cls):
        """fetch_unread_emails must pass timeout=30 by default to IMAPClient."""
        mock_client = MagicMock()
        mock_client.search.return_value = []
        mock_imap_cls.return_value.__enter__.return_value = mock_client

        agent_email.fetch_unread_emails()

        mock_imap_cls.assert_called_once_with(
            "imap.example.com",
            ssl=True,
            timeout=agent_email.DEFAULT_IMAP_TIMEOUT,
        )
        self.assertEqual(agent_email.DEFAULT_IMAP_TIMEOUT, 30)

    @patch("agent_email.IMAPClient")
    def test_fetch_unread_emails_accepts_custom_timeout(self, mock_imap_cls):
        """fetch_unread_emails must pass custom timeout through to IMAPClient."""
        mock_client = MagicMock()
        mock_client.search.return_value = []
        mock_imap_cls.return_value.__enter__.return_value = mock_client

        agent_email.fetch_unread_emails(timeout=45)

        mock_imap_cls.assert_called_once_with("imap.example.com", ssl=True, timeout=45)

    @patch("agent_email.IMAPClient")
    def test_fetch_attachment_content_uses_timeout(self, mock_imap_cls):
        """fetch_attachment_content_by_uid must pass timeout to IMAPClient."""
        mock_client = MagicMock()
        mock_client.fetch.return_value = {}
        mock_imap_cls.return_value.__enter__.return_value = mock_client

        agent_email.fetch_attachment_content_by_uid("123", "invoice.pdf")

        mock_imap_cls.assert_called_once_with(
            "imap.example.com",
            ssl=True,
            timeout=agent_email.DEFAULT_IMAP_TIMEOUT,
        )

    @patch("agent_email.IMAPClient")
    def test_mark_emails_as_read_uses_timeout(self, mock_imap_cls):
        """mark_emails_as_read must pass timeout to IMAPClient."""
        mock_client = MagicMock()
        mock_imap_cls.return_value.__enter__.return_value = mock_client

        agent_email.mark_emails_as_read([101, 102])

        mock_imap_cls.assert_called_once_with(
            "imap.example.com",
            ssl=True,
            timeout=agent_email.DEFAULT_IMAP_TIMEOUT,
        )


class TestSec05UnreadEmailCapping(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {
            "EMAIL_IMAP_SERVER": "imap.example.com",
            "EMAIL_ADDRESS": "family@example.com",
            "EMAIL_APP_PASSWORD": "secret_password",
        })
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    @patch("agent_email.IMAPClient")
    def test_fetch_unread_emails_caps_batch_to_20_latest(self, mock_imap_cls):
        """When 50 unread emails are found, only the last 20 are fetched."""
        mock_client = MagicMock()
        all_uids = list(range(1, 51))  # 1 to 50
        mock_client.search.return_value = all_uids
        mock_client.fetch.return_value = {}
        mock_imap_cls.return_value.__enter__.return_value = mock_client

        agent_email.fetch_unread_emails()

        # Check what was passed to client.fetch
        mock_client.fetch.assert_called_once()
        args, _ = mock_client.fetch.call_args
        fetched_uids = args[0]
        self.assertEqual(len(fetched_uids), 20)
        self.assertEqual(fetched_uids, list(range(31, 51)))

    @patch("agent_email.IMAPClient")
    def test_fetch_unread_emails_custom_batch_cap(self, mock_imap_cls):
        """fetch_unread_emails honors custom max_emails limit."""
        mock_client = MagicMock()
        mock_client.search.return_value = list(range(1, 30))
        mock_client.fetch.return_value = {}
        mock_imap_cls.return_value.__enter__.return_value = mock_client

        agent_email.fetch_unread_emails(max_emails=5)

        args, _ = mock_client.fetch.call_args
        fetched_uids = args[0]
        self.assertEqual(len(fetched_uids), 5)
        self.assertEqual(fetched_uids, list(range(25, 30)))

    @patch("agent_email.IMAPClient")
    def test_fetch_unread_emails_batch_fetch_fallback(self, mock_imap_cls):
        """If batch client.fetch raises an error, fallback to fetching per-message."""
        mock_client = MagicMock()
        mock_client.search.return_value = [10, 11]

        # First call (batch) raises exception, subsequent single calls succeed for 11 but fail for 10
        def fetch_side_effect(uids, parts):
            if uids == [10, 11]:
                raise RuntimeError("Batch MIME parse failed on server")
            elif uids == [10]:
                raise RuntimeError("Corrupt message 10")
            elif uids == [11]:
                return {11: {b"RFC822": b"From: sender@example.com\r\nSubject: Hi\r\n\r\nHello"}}
            return {}

        mock_client.fetch.side_effect = fetch_side_effect
        mock_imap_cls.return_value.__enter__.return_value = mock_client

        results = agent_email.fetch_unread_emails()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["uid"], "11")
        self.assertEqual(results[0]["subject"], "Hi")


class TestSec05PdfExtractionLimits(unittest.TestCase):
    def test_extract_pdf_text_rejects_exceeding_max_bytes(self):
        """extract_pdf_text raises ValueError when input byte length exceeds max_bytes."""
        oversized_bytes = b"%PDF-1.4 " + (b"0" * 100)
        with self.assertRaises(ValueError) as ctx:
            agent_email.extract_pdf_text(oversized_bytes, max_bytes=50)
        self.assertIn("exceeds maximum allowed limit", str(ctx.exception))

    @patch("agent_email.PdfReader")
    def test_extract_pdf_text_respects_max_pages(self, mock_reader_cls):
        """extract_pdf_text caps page extraction at max_pages."""
        mock_pages = []
        for i in range(50):
            page = MagicMock()
            page.extract_text.return_value = f"Page {i} text."
            mock_pages.append(page)

        mock_reader = MagicMock()
        mock_reader.pages = mock_pages
        mock_reader_cls.return_value = mock_reader

        result = agent_email.extract_pdf_text(b"%PDF-1.4 valid", max_pages=10)

        # Pages 0 through 9 should be extracted, page 10+ not touched
        self.assertIn("Page 0 text.", result)
        self.assertIn("Page 9 text.", result)
        self.assertNotIn("Page 10 text.", result)
        self.assertEqual(mock_pages[0].extract_text.call_count, 1)
        self.assertEqual(mock_pages[9].extract_text.call_count, 1)
        self.assertEqual(mock_pages[10].extract_text.call_count, 0)

    @patch("agent_email.PdfReader")
    def test_extract_pdf_text_respects_max_chars(self, mock_reader_cls):
        """extract_pdf_text truncates extracted text when max_chars is reached."""
        mock_pages = []
        for i in range(10):
            page = MagicMock()
            page.extract_text.return_value = "ABCDE" * 20  # 100 characters per page
            mock_pages.append(page)

        mock_reader = MagicMock()
        mock_reader.pages = mock_pages
        mock_reader_cls.return_value = mock_reader

        result = agent_email.extract_pdf_text(b"%PDF-1.4 valid", max_chars=250)

        self.assertLessEqual(len(result), 260)  # Account for join spaces
        # Page 3 onwards should not be fully extracted
        self.assertEqual(mock_pages[0].extract_text.call_count, 1)
        self.assertEqual(mock_pages[1].extract_text.call_count, 1)
        self.assertEqual(mock_pages[2].extract_text.call_count, 1)
        self.assertEqual(mock_pages[3].extract_text.call_count, 0)

    def test_extract_pdf_text_handles_corrupted_pdf_gracefully(self):
        """extract_pdf_text returns empty string and does not crash on corrupt PDF bytes."""
        corrupt_data = b"GARBAGE_NOT_A_VALID_PDF_HEADER_1234567890"
        result = agent_email.extract_pdf_text(corrupt_data)
        self.assertEqual(result, "")


class TestSec05ExtractPdfFileTool(unittest.TestCase):
    def setUp(self):
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        self.mock_ctx = MagicMock(spec=FamilySystemContext)

    @patch("os.path.getsize")
    def test_extract_pdf_file_rejects_oversized_file(self, mock_getsize):
        """extract_pdf_file returns error without opening if file size exceeds 20MB."""
        test_file = os.path.join(DOWNLOADS_DIR, "large_test.pdf")
        with open(test_file, "wb") as f:
            f.write(b"%PDF-1.4 dummy header")

        try:
            # Simulate 25MB file
            mock_getsize.return_value = 25 * 1024 * 1024
            res = main.extract_pdf_file(self.mock_ctx, "large_test.pdf")
            self.assertTrue(res.startswith("Error:"))
            self.assertIn("exceeds maximum allowed limit", res)
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

    @patch("main.extract_pdf_text")
    def test_extract_pdf_file_within_size_limit_extracts_text(self, mock_extract):
        """extract_pdf_file extracts text when file size is within limits."""
        test_file = os.path.join(DOWNLOADS_DIR, "normal_test.pdf")
        with open(test_file, "wb") as f:
            f.write(b"%PDF-1.4 normal content")

        try:
            mock_extract.return_value = "Extracted document schedule text"
            res = main.extract_pdf_file(self.mock_ctx, "normal_test.pdf")
            self.assertEqual(res, "Extracted document schedule text")
            mock_extract.assert_called_once()
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)


class TestSec05DownloadAttachmentSizeLimit(unittest.TestCase):
    def setUp(self):
        self.mock_ctx = MagicMock(spec=FamilySystemContext)

    @patch("main.fetch_attachment_content_by_uid")
    @patch("email_store.get_downloaded_path")
    def test_download_attachment_rejects_oversized_content(self, mock_cached, mock_fetch):
        """download_attachment does not save files exceeding MAX_ATTACHMENT_SIZE_BYTES."""
        mock_cached.return_value = None
        # Return 25MB dummy payload
        mock_fetch.return_value = b"X" * (25 * 1024 * 1024)

        res = main.download_attachment(self.mock_ctx, "123", "huge.bin")

        self.assertTrue(res.startswith("Error:"))
        self.assertIn("exceeds maximum limit", res)


class TestSec05TelegramDocumentSizeLimit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
        self.context.bot = AsyncMock()

    def _make_update_with_doc(self, file_size: int, file_name: str = "statement.pdf"):
        update = MagicMock(spec=Update)
        chat = MagicMock(spec=Chat)
        chat.type = constants.ChatType.PRIVATE
        update.effective_chat = chat

        user = MagicMock(spec=User)
        user.id = 12345
        user.first_name = "Alice"
        user.username = "alice"
        user.last_name = None
        update.effective_user = user

        message = MagicMock(spec=Message)
        message.text = None
        message.caption = None
        message.photo = None

        doc = MagicMock(spec=Document)
        doc.file_id = "doc_file_123"
        doc.file_name = file_name
        doc.file_size = file_size
        doc.mime_type = "application/pdf"
        message.document = doc

        update.message = message
        return update

    @patch("main.ALLOWED_USER_IDS", [12345])
    @patch("main.safe_reply", new_callable=AsyncMock)
    @patch("main.run_agent", new_callable=AsyncMock)
    async def test_handle_message_rejects_oversized_document(self, mock_run_agent, mock_safe_reply):
        """handle_message does not download documents larger than 20MB and alerts the user."""
        # 25MB document
        update = self._make_update_with_doc(25 * 1024 * 1024, "big_doc.pdf")

        await main.handle_message(update, self.context)

        self.context.bot.get_file.assert_not_called()
        mock_safe_reply.assert_called_once()
        args, _ = mock_safe_reply.call_args
        self.assertIn("exceeds the maximum allowed size of 20MB", args[1])
        mock_run_agent.assert_not_called()

    @patch("main.ALLOWED_USER_IDS", [12345])
    @patch("main.safe_reply", new_callable=AsyncMock)
    @patch("main.run_agent", new_callable=AsyncMock)
    async def test_handle_message_rejects_oversized_file_from_bot(self, mock_run_agent, mock_safe_reply):
        """handle_message checks file.file_size if doc.file_size was None/missing."""
        update = self._make_update_with_doc(0, "big_stream.pdf")
        update.message.document.file_size = None  # Missing size in document metadata

        mock_file = AsyncMock(spec=File)
        mock_file.file_size = 25 * 1024 * 1024  # Size reported by Bot.get_file
        self.context.bot.get_file.return_value = mock_file

        await main.handle_message(update, self.context)

        mock_file.download_as_bytearray.assert_not_called()
        mock_safe_reply.assert_called_once()
        args, _ = mock_safe_reply.call_args
        self.assertIn("exceeds the maximum allowed size of 20MB", args[1])
        mock_run_agent.assert_not_called()


class TestSec05PoisonPillProtection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._email_failure_counts.clear()
        self.context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
        self.context.bot = AsyncMock()

    def tearDown(self):
        main._email_failure_counts.clear()

    @patch("main.ALLOWED_USER_IDS", [12345])
    @patch("main.agent_email.fetch_unread_emails")
    @patch("main.agent_email.mark_emails_as_read")
    @patch("main.email_store.store_email")
    @patch("main.read_profile")
    @patch("main.run_agent", new_callable=AsyncMock)
    async def test_repeated_failure_marks_poison_pill_as_read_and_skips(
        self,
        mock_run_agent,
        mock_read_profile,
        mock_store,
        mock_mark_read,
        mock_fetch_emails,
    ):
        """Email UID that has reached MAX_EMAIL_FAILURES is marked as read and skipped."""
        main._email_failure_counts["999"] = main.MAX_EMAIL_FAILURES  # Poison pill

        mock_fetch_emails.return_value = [
            {"uid": "999", "sender": "bad@example.com", "subject": "Crash", "body_snippet": "Bad", "attachments": []},
            {"uid": "1000", "sender": "good@example.com", "subject": "Good", "body_snippet": "OK", "attachments": []},
        ]
        mock_read_profile.return_value = {}
        mock_run_agent.return_value = "Processed successfully"

        await main.check_email_job(self.context)

        # 999 should be marked as read immediately to break crash loop
        mock_mark_read.assert_any_call([999])
        self.assertNotIn("999", main._email_failure_counts)

        # 1000 was processed through run_agent
        mock_run_agent.assert_called_once()
        args, _ = mock_run_agent.call_args
        prompt_content = args[0]
        self.assertIn("good@example.com", prompt_content)
        self.assertNotIn("bad@example.com", prompt_content)

    @patch("main.ALLOWED_USER_IDS", [12345])
    @patch("main.agent_email.fetch_unread_emails")
    @patch("main.agent_email.mark_emails_as_read")
    @patch("main.email_store.store_email")
    @patch("main.read_profile")
    @patch("main.extract_pdf_text")
    @patch("main.fetch_attachment_content_by_uid")
    @patch("main.run_agent", new_callable=AsyncMock)
    async def test_malformed_attachment_does_not_abort_batch(
        self,
        mock_run_agent,
        mock_fetch_att,
        mock_extract_pdf,
        mock_read_profile,
        mock_store,
        mock_mark_read,
        mock_fetch_emails,
    ):
        """A failing attachment extraction in one email does not crash the check loop."""
        mock_fetch_emails.return_value = [
            {
                "uid": "101",
                "sender": "sender1@example.com",
                "subject": "Email with bad PDF",
                "body_snippet": "See bad attachment",
                "attachments": [{"filename": "bad.pdf", "mime_type": "application/pdf", "size_bytes": 1024}],
            },
            {
                "uid": "102",
                "sender": "sender2@example.com",
                "subject": "Normal email",
                "body_snippet": "Meeting at 3pm",
                "attachments": [],
            },
        ]
        mock_read_profile.return_value = {}
        mock_fetch_att.return_value = b"%PDF-corrupt"
        mock_extract_pdf.side_effect = RuntimeError("PDF decompression bomb error")
        mock_run_agent.return_value = "No urgent updates"

        await main.check_email_job(self.context)

        mock_run_agent.assert_called_once()
        # Both emails are marked read after completion
        mock_mark_read.assert_called_with([101, 102])
        self.assertEqual(len(main._email_failure_counts), 0)


if __name__ == "__main__":
    unittest.main()
