import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import main
from main import (
    DOWNLOADS_DIR,
    FamilySystemContext,
    _resolve_download_path,
    extract_pdf_file,
    send_file_to_chat,
)


class TestSec02ResolveDownloadPath(unittest.TestCase):
    def setUp(self):
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    def test_empty_and_whitespace_paths_rejected(self):
        """Empty or whitespace-only paths must raise ValueError."""
        for empty_path in ["", "   ", "\t", "\n"]:
            with self.assertRaises(ValueError) as ctx:
                _resolve_download_path(empty_path)
            self.assertIn("File path cannot be empty", str(ctx.exception))

    def test_absolute_paths_outside_downloads_rejected(self):
        """Absolute paths outside downloads/ must be strictly rejected."""
        forbidden_paths = [
            "/etc/passwd",
            "/etc/shadow",
            os.path.join(main.BASE_DIR, ".env"),
            os.path.join(main.BASE_DIR, "main.py"),
            "/tmp/secret_file.txt",
        ]
        for path in forbidden_paths:
            with self.assertRaises(ValueError) as ctx:
                _resolve_download_path(path)
            self.assertIn("Access denied", str(ctx.exception))
            self.assertIn("outside authorized downloads directory", str(ctx.exception))

    def test_relative_path_traversal_rejected(self):
        """Path traversal sequences (..) escaping downloads/ must be rejected."""
        traversal_attempts = [
            "../.env",
            "../../etc/passwd",
            "../../../secret.txt",
            "downloads/../.env",
            "downloads/../../etc/passwd",
            "./downloads/../.env",
            "./downloads/../../etc/passwd",
        ]
        for path in traversal_attempts:
            with self.assertRaises(ValueError) as ctx:
                _resolve_download_path(path)
            self.assertIn("Access denied", str(ctx.exception))
            self.assertIn("outside authorized downloads directory", str(ctx.exception))

    def test_sibling_directory_rejected(self):
        """Paths pointing to sibling directories like downloads_backup/ must be rejected."""
        sibling_attempts = [
            "/home/mio/src/hozbot/downloads_backup/secret.txt",
            os.path.join(main.BASE_DIR, "downloads_backup", "secret.txt"),
            "../downloads_backup/secret.txt",
        ]
        for path in sibling_attempts:
            with self.assertRaises(ValueError) as ctx:
                _resolve_download_path(path)
            self.assertIn("Access denied", str(ctx.exception))

    def test_downloads_directory_itself_rejected(self):
        """Targeting the downloads directory itself as a file must be rejected."""
        dir_targets = [
            "downloads",
            "./downloads",
            "downloads/",
            "./downloads/",
            DOWNLOADS_DIR,
        ]
        for path in dir_targets:
            with self.assertRaises(ValueError) as ctx:
                _resolve_download_path(path)
            self.assertIn("Access denied", str(ctx.exception))

    def test_directory_inside_downloads_rejected(self):
        """Directories inside downloads/ must be rejected when targeted as files."""
        sub_dir = os.path.join(DOWNLOADS_DIR, "test_sub_dir")
        os.makedirs(sub_dir, exist_ok=True)
        try:
            with self.assertRaises(ValueError) as ctx:
                _resolve_download_path("test_sub_dir")
            self.assertIn("is a directory, not a file", str(ctx.exception))

            with self.assertRaises(ValueError) as ctx:
                _resolve_download_path(os.path.join("downloads", "test_sub_dir"))
            self.assertIn("is a directory, not a file", str(ctx.exception))
        finally:
            if os.path.exists(sub_dir):
                os.rmdir(sub_dir)

    def test_symlink_pointing_outside_downloads_rejected(self):
        """Symlinks inside downloads/ resolving to outside files must be strictly rejected."""
        outside_target = os.path.join(main.BASE_DIR, "pyproject.toml")
        symlink_path = os.path.join(DOWNLOADS_DIR, "evil_symlink.pdf")
        if os.path.exists(symlink_path):
            os.remove(symlink_path)
        os.symlink(outside_target, symlink_path)

        try:
            with self.assertRaises(ValueError) as ctx:
                _resolve_download_path("evil_symlink.pdf")
            self.assertIn("Access denied", str(ctx.exception))

            with self.assertRaises(ValueError) as ctx:
                _resolve_download_path(symlink_path)
            self.assertIn("Access denied", str(ctx.exception))
        finally:
            if os.path.islink(symlink_path) or os.path.exists(symlink_path):
                os.remove(symlink_path)

    def test_legitimate_files_inside_downloads_allowed(self):
        """Legitimate file paths inside downloads/ must resolve correctly."""
        test_file = os.path.join(DOWNLOADS_DIR, "legit_test.pdf")
        with open(test_file, "w") as f:
            f.write("dummy content")

        canonical_test_file = os.path.realpath(test_file)
        try:
            # 1. Plain filename
            self.assertEqual(_resolve_download_path("legit_test.pdf"), canonical_test_file)
            # 2. downloads/ prefix
            self.assertEqual(_resolve_download_path("downloads/legit_test.pdf"), canonical_test_file)
            # 3. ./downloads/ prefix
            self.assertEqual(_resolve_download_path("./downloads/legit_test.pdf"), canonical_test_file)
            # 4. Absolute path
            self.assertEqual(_resolve_download_path(test_file), canonical_test_file)
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

    def test_symlink_pointing_inside_downloads_allowed(self):
        """Symlinks inside downloads/ pointing to files inside downloads/ are allowed."""
        real_file = os.path.join(DOWNLOADS_DIR, "real_target.pdf")
        link_file = os.path.join(DOWNLOADS_DIR, "alias_link.pdf")
        with open(real_file, "w") as f:
            f.write("target content")
        if os.path.exists(link_file):
            os.remove(link_file)
        os.symlink(real_file, link_file)

        try:
            canonical_real = os.path.realpath(real_file)
            self.assertEqual(_resolve_download_path("alias_link.pdf"), canonical_real)
            self.assertEqual(_resolve_download_path(link_file), canonical_real)
        finally:
            if os.path.islink(link_file) or os.path.exists(link_file):
                os.remove(link_file)
            if os.path.exists(real_file):
                os.remove(real_file)

    def test_nested_file_inside_downloads_subdirectory_allowed(self):
        """Files located within subdirectories of downloads/ are allowed."""
        sub_dir = os.path.join(DOWNLOADS_DIR, "nested_folder")
        os.makedirs(sub_dir, exist_ok=True)
        nested_file = os.path.join(sub_dir, "nested_doc.pdf")
        with open(nested_file, "w") as f:
            f.write("nested content")

        canonical_nested = os.path.realpath(nested_file)
        try:
            self.assertEqual(_resolve_download_path("nested_folder/nested_doc.pdf"), canonical_nested)
            self.assertEqual(_resolve_download_path("downloads/nested_folder/nested_doc.pdf"), canonical_nested)
            self.assertEqual(_resolve_download_path(nested_file), canonical_nested)
        finally:
            if os.path.exists(nested_file):
                os.remove(nested_file)
            if os.path.exists(sub_dir):
                os.rmdir(sub_dir)


class TestSec02ExtractPdfFile(unittest.TestCase):
    def setUp(self):
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        self.mock_ctx = MagicMock()
        self.mock_ctx.deps = FamilySystemContext(
            user_id=12345, username="tester", first_name="Test", last_name="User"
        )

    def test_extract_pdf_rejects_external_paths(self):
        """extract_pdf_file must return an error and not open files outside downloads/."""
        bad_paths = [
            "/etc/passwd",
            os.path.join(main.BASE_DIR, ".env"),
            "../.env",
            "../../etc/passwd",
            "downloads/../.env",
        ]
        for path in bad_paths:
            res = extract_pdf_file(self.mock_ctx, path)
            self.assertTrue(res.startswith("Error:"), f"Expected error for '{path}', got: {res}")
            self.assertIn("Access denied", res)

    def test_extract_pdf_rejects_symlink_outside(self):
        """extract_pdf_file must reject symlinks escaping downloads/."""
        outside_target = os.path.join(main.BASE_DIR, "pyproject.toml")
        symlink_path = os.path.join(DOWNLOADS_DIR, "leak_symlink.pdf")
        if os.path.exists(symlink_path):
            os.remove(symlink_path)
        os.symlink(outside_target, symlink_path)

        try:
            res = extract_pdf_file(self.mock_ctx, "leak_symlink.pdf")
            self.assertTrue(res.startswith("Error:"), f"Expected error, got: {res}")
            self.assertIn("Access denied", res)
        finally:
            if os.path.islink(symlink_path) or os.path.exists(symlink_path):
                os.remove(symlink_path)

    def test_extract_pdf_file_not_found(self):
        """extract_pdf_file returns File not found when file does not exist inside downloads/."""
        res = extract_pdf_file(self.mock_ctx, "downloads/non_existent_file_9999.pdf")
        self.assertTrue(res.startswith("File not found:"))

    def test_extract_pdf_rejects_directory(self):
        """extract_pdf_file returns error when given a directory."""
        res = extract_pdf_file(self.mock_ctx, "downloads")
        self.assertTrue(res.startswith("Error:"))
        self.assertIn("Access denied", res)

    @patch("main.extract_pdf_text")
    def test_extract_pdf_success_for_valid_file(self, mock_extract):
        """extract_pdf_file successfully extracts text for a valid file inside downloads/."""
        mock_extract.return_value = "Extracted PDF schedule content"
        test_file = os.path.join(DOWNLOADS_DIR, "valid_test.pdf")
        with open(test_file, "wb") as f:
            f.write(b"%PDF-1.4 dummy content")

        try:
            res = extract_pdf_file(self.mock_ctx, "valid_test.pdf")
            self.assertEqual(res, "Extracted PDF schedule content")
            mock_extract.assert_called_once()
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)


class TestSec02SendFileToChat(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        self.mock_bot = AsyncMock()
        main._telegram_bot = self.mock_bot
        self.mock_ctx = MagicMock()
        self.mock_ctx.deps = FamilySystemContext(
            user_id=12345, username="tester", first_name="Test", last_name="User"
        )

    def tearDown(self):
        main._telegram_bot = None

    async def test_send_file_to_chat_rejects_system_context(self):
        """send_file_to_chat must refuse sending when user_id == 0 (system context)."""
        self.mock_ctx.deps.user_id = 0
        res = await send_file_to_chat(self.mock_ctx, "downloads/test.pdf")
        self.assertEqual(res, "Error: Cannot send files in system context.")
        self.mock_bot.send_document.assert_not_called()

    async def test_send_file_to_chat_rejects_none_bot(self):
        """send_file_to_chat returns error if bot is not available."""
        main._telegram_bot = None
        res = await send_file_to_chat(self.mock_ctx, "downloads/test.pdf")
        self.assertEqual(res, "Error: Telegram bot not available.")

    async def test_send_file_to_chat_rejects_external_paths(self):
        """send_file_to_chat must reject external paths without calling send_document."""
        bad_paths = [
            "/etc/passwd",
            os.path.join(main.BASE_DIR, ".env"),
            "../.env",
            "../../etc/passwd",
            "downloads/../.env",
        ]
        for path in bad_paths:
            res = await send_file_to_chat(self.mock_ctx, path)
            self.assertTrue(res.startswith("Error:"), f"Expected error for '{path}', got: {res}")
            self.assertIn("Access denied", res)
            self.mock_bot.send_document.assert_not_called()

    async def test_send_file_to_chat_rejects_symlink_outside(self):
        """send_file_to_chat must reject symlinks pointing outside downloads/."""
        outside_target = os.path.join(main.BASE_DIR, "pyproject.toml")
        symlink_path = os.path.join(DOWNLOADS_DIR, "leak_symlink.pdf")
        if os.path.exists(symlink_path):
            os.remove(symlink_path)
        os.symlink(outside_target, symlink_path)

        try:
            res = await send_file_to_chat(self.mock_ctx, "leak_symlink.pdf")
            self.assertTrue(res.startswith("Error:"), f"Expected error, got: {res}")
            self.assertIn("Access denied", res)
            self.mock_bot.send_document.assert_not_called()
        finally:
            if os.path.islink(symlink_path) or os.path.exists(symlink_path):
                os.remove(symlink_path)

    async def test_send_file_to_chat_file_not_found(self):
        """send_file_to_chat returns File not found when file does not exist inside downloads/."""
        res = await send_file_to_chat(self.mock_ctx, "downloads/non_existent_9999.png")
        self.assertTrue(res.startswith("File not found:"))
        self.mock_bot.send_document.assert_not_called()

    async def test_send_file_to_chat_rejects_directory(self):
        """send_file_to_chat returns error when path is a directory."""
        res = await send_file_to_chat(self.mock_ctx, "downloads")
        self.assertTrue(res.startswith("Error:"))
        self.assertIn("Access denied", res)
        self.mock_bot.send_document.assert_not_called()

    async def test_send_file_to_chat_success_for_valid_file(self):
        """send_file_to_chat sends document to chat_id and returns confirmation."""
        test_file = os.path.join(DOWNLOADS_DIR, "photo.jpg")
        with open(test_file, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0 dummy jpeg")

        try:
            res = await send_file_to_chat(self.mock_ctx, "photo.jpg", caption="Your file")
            self.assertEqual(res, "Sent: photo.jpg")
            self.mock_bot.send_document.assert_called_once()
            _, kwargs = self.mock_bot.send_document.call_args
            self.assertEqual(kwargs["chat_id"], 12345)
            self.assertEqual(kwargs["caption"], "Your file")
            self.assertEqual(kwargs["read_timeout"], 30)
            self.assertEqual(kwargs["write_timeout"], 30)
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

    async def test_send_file_to_chat_handles_telegram_error(self):
        """send_file_to_chat catches Telegram exceptions and returns error message."""
        self.mock_bot.send_document.side_effect = RuntimeError("Telegram network failure")
        test_file = os.path.join(DOWNLOADS_DIR, "photo.jpg")
        with open(test_file, "wb") as f:
            f.write(b"data")

        try:
            res = await send_file_to_chat(self.mock_ctx, "photo.jpg")
            self.assertTrue(res.startswith("Error sending file: Telegram network failure"))
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)
