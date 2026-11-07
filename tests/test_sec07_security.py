import json
import os
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import bin_collection
import database
import email_store
import google_calendar
from google_calendar import GoogleCalendar, migrate_from_json
import main
import memory


def _get_mode(path: str) -> int:
    """Return POSIX permission bits (e.g. 0o700, 0o600)."""
    return stat.S_IMODE(os.stat(path).st_mode)


class TestEnforceStoragePermissions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_dir = os.path.join(self.temp_dir.name, "storage")
        self.env_path = os.path.join(self.temp_dir.name, ".env")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_enforce_storage_permissions_creates_missing_storage_dir(self):
        """Must create storage directory with 0700 permissions if it does not exist."""
        self.assertFalse(os.path.exists(self.storage_dir))
        main.enforce_storage_permissions(self.storage_dir, self.env_path)
        self.assertTrue(os.path.isdir(self.storage_dir))
        self.assertEqual(_get_mode(self.storage_dir), 0o700)

    def test_enforce_storage_permissions_hardens_existing_storage_dir(self):
        """Must restrict an existing 0755 or 0777 storage directory to 0700."""
        os.makedirs(self.storage_dir, mode=0o755, exist_ok=True)
        os.chmod(self.storage_dir, 0o755)
        self.assertEqual(_get_mode(self.storage_dir), 0o755)

        main.enforce_storage_permissions(self.storage_dir, self.env_path)
        self.assertEqual(_get_mode(self.storage_dir), 0o700)

    def test_enforce_storage_permissions_hardens_files_in_storage(self):
        """Must restrict world-readable (0644/0666) files inside storage to 0600."""
        os.makedirs(self.storage_dir, mode=0o700, exist_ok=True)

        sample_files = [
            "family_profile.json",
            "google_calendar_token.json",
            "hozbot.db",
            "thread_memory.json",
            "last_digest.json",
            "skipped_events.json",
            "bin_cache.json",
        ]
        for name in sample_files:
            file_path = os.path.join(self.storage_dir, name)
            with open(file_path, "w") as f:
                f.write("test content")
            os.chmod(file_path, 0o644)
            self.assertEqual(_get_mode(file_path), 0o644)

        main.enforce_storage_permissions(self.storage_dir, self.env_path)

        for name in sample_files:
            file_path = os.path.join(self.storage_dir, name)
            self.assertEqual(
                _get_mode(file_path),
                0o600,
                f"File {name} should have 0600 permissions",
            )

    def test_enforce_storage_permissions_hardens_env_file(self):
        """Must restrict .env file to 0600 if present."""
        with open(self.env_path, "w") as f:
            f.write("TELEGRAM_BOT_TOKEN=secret\n")
        os.chmod(self.env_path, 0o644)
        self.assertEqual(_get_mode(self.env_path), 0o644)

        main.enforce_storage_permissions(self.storage_dir, self.env_path)
        self.assertEqual(_get_mode(self.env_path), 0o600)

    def test_enforce_storage_permissions_missing_env_handled_gracefully(self):
        """Must not fail if .env is missing."""
        self.assertFalse(os.path.exists(self.env_path))
        main.enforce_storage_permissions(self.storage_dir, self.env_path)
        self.assertTrue(os.path.isdir(self.storage_dir))
        self.assertEqual(_get_mode(self.storage_dir), 0o700)

    def test_enforce_storage_permissions_hardens_storage_subdirectories(self):
        """Must restrict any subdirectories inside storage to 0700."""
        os.makedirs(self.storage_dir, mode=0o700, exist_ok=True)
        sub_dir = os.path.join(self.storage_dir, "backup")
        os.makedirs(sub_dir, mode=0o755, exist_ok=True)
        os.chmod(sub_dir, 0o755)

        main.enforce_storage_permissions(self.storage_dir, self.env_path)
        self.assertEqual(_get_mode(sub_dir), 0o700)


class TestDatabasePermissions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.profile_path = os.path.join(self.temp_dir.name, "storage", "family_profile.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_update_json_creates_0600_file_and_0700_dir(self):
        """_update_json must create the parent directory as 0700 and the JSON file as 0600."""
        database._update_json(self.profile_path, lambda d: {"critical_notes": ["fact1"]})

        dir_path = os.path.dirname(self.profile_path)
        self.assertTrue(os.path.isdir(dir_path))
        self.assertEqual(_get_mode(dir_path), 0o700)
        self.assertTrue(os.path.isfile(self.profile_path))
        self.assertEqual(_get_mode(self.profile_path), 0o600)

    def test_update_json_atomic_tmp_file_permissions(self):
        """_update_json must create the temporary file with 0600 permissions."""
        captured_tmp_mode = None

        original_open = os.open

        def spy_open(path, flags, mode=0o777):
            fd = original_open(path, flags, mode)
            if str(path).endswith(".tmp"):
                nonlocal captured_tmp_mode
                captured_tmp_mode = stat.S_IMODE(os.fstat(fd).st_mode)
            return fd

        with patch("os.open", side_effect=spy_open):
            database._update_json(self.profile_path, lambda d: {"note": "test"})

        self.assertIsNotNone(captured_tmp_mode, "A .tmp file must be created during atomic write")
        self.assertEqual(captured_tmp_mode, 0o600, "Atomic .tmp file must have 0600 permissions")
        self.assertEqual(_get_mode(self.profile_path), 0o600)

    def test_read_profile_initializes_0600_file_when_missing(self):
        """read_profile must initialize family_profile.json with 0600 permissions if missing."""
        with patch.object(database, "PROFILE_PATH", self.profile_path):
            self.assertFalse(os.path.exists(self.profile_path))
            profile = database.read_profile()
            self.assertEqual(profile, {})
            self.assertTrue(os.path.isfile(self.profile_path))
            self.assertEqual(_get_mode(self.profile_path), 0o600)

    def test_append_fact_maintains_0600_permissions(self):
        """append_fact must ensure family_profile.json remains 0600."""
        with patch.object(database, "PROFILE_PATH", self.profile_path):
            database.append_fact("New test fact")
            self.assertEqual(_get_mode(self.profile_path), 0o600)
            database.append_fact("Another fact")
            self.assertEqual(_get_mode(self.profile_path), 0o600)

            profile = database.read_profile()
            self.assertIn("New test fact", profile["critical_notes"])
            self.assertIn("Another fact", profile["critical_notes"])

    def test_database_permissions_under_permissive_umask(self):
        """_update_json must enforce 0600 even when process umask is 0000."""
        old_umask = os.umask(0)
        try:
            database._update_json(self.profile_path, lambda d: {"key": "val"})
            self.assertEqual(_get_mode(self.profile_path), 0o600)
            self.assertEqual(_get_mode(os.path.dirname(self.profile_path)), 0o700)
        finally:
            os.umask(old_umask)


class TestMemoryPermissions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.memory_path = os.path.join(self.temp_dir.name, "storage", "thread_memory.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_memory_creates_0600_file_and_0700_dir(self):
        """_save_memory must create parent dir as 0700 and memory file as 0600."""
        with patch.object(memory, "MEMORY_FILE", self.memory_path):
            memory._save_memory({"12345": [{"text": "hello", "ts": "2026-11-07T12:00:00Z"}]})

            dir_path = os.path.dirname(self.memory_path)
            self.assertTrue(os.path.isdir(dir_path))
            self.assertEqual(_get_mode(dir_path), 0o700)
            self.assertTrue(os.path.isfile(self.memory_path))
            self.assertEqual(_get_mode(self.memory_path), 0o600)

    def test_add_message_creates_0600_file(self):
        """add_message must write thread_memory.json with 0600 permissions."""
        with patch.object(memory, "MEMORY_FILE", self.memory_path):
            memory.add_message(101, "Message 1")
            self.assertEqual(_get_mode(self.memory_path), 0o600)
            memory.add_message(101, "Message 2")
            self.assertEqual(_get_mode(self.memory_path), 0o600)

            msgs = memory.get_memory(101)
            self.assertIn("Message 1", msgs)
            self.assertIn("Message 2", msgs)

    def test_memory_permissions_under_permissive_umask(self):
        """_save_memory must enforce 0600 even when process umask is 0000."""
        old_umask = os.umask(0)
        try:
            with patch.object(memory, "MEMORY_FILE", self.memory_path):
                memory._save_memory({"test": []})
                self.assertEqual(_get_mode(self.memory_path), 0o600)
                self.assertEqual(_get_mode(os.path.dirname(self.memory_path)), 0o700)
        finally:
            os.umask(old_umask)


class TestGoogleCalendarPermissions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.token_path = os.path.join(self.temp_dir.name, "storage", "google_calendar_token.json")
        self.creds_path = os.path.join(self.temp_dir.name, "storage", "credentials.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_token_creates_0600_file_and_0700_dir(self):
        """GoogleCalendar._save_token must write the token file with 0600 permissions and dir with 0700."""
        gc = GoogleCalendar(self.creds_path, self.token_path)

        mock_creds = MagicMock()
        mock_creds.to_json.return_value = json.dumps({"token": "ya29.test-secret-token"})

        gc._save_token(mock_creds)

        dir_path = os.path.dirname(self.token_path)
        self.assertTrue(os.path.isdir(dir_path))
        self.assertEqual(_get_mode(dir_path), 0o700)
        self.assertTrue(os.path.isfile(self.token_path))
        self.assertEqual(_get_mode(self.token_path), 0o600)

        with open(self.token_path) as f:
            data = json.load(f)
        self.assertEqual(data["token"], "ya29.test-secret-token")

    def test_try_load_token_persists_refreshed_token_with_0600(self):
        """When try_load_token refreshes an expired token, it must save with 0600 permissions."""
        os.makedirs(os.path.dirname(self.token_path), mode=0o700, exist_ok=True)
        with open(self.token_path, "w") as f:
            f.write(json.dumps({"token": "old"}))
        os.chmod(self.token_path, 0o644)

        gc = GoogleCalendar(self.creds_path, self.token_path)

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "refresh-secret"
        mock_creds.to_json.return_value = json.dumps({"token": "new-refreshed"})

        with patch("google_calendar.Credentials.from_authorized_user_file", return_value=mock_creds), \
             patch("google_calendar.build") as mock_build:
            success = gc.try_load_token()

        self.assertTrue(success)
        mock_creds.refresh.assert_called_once()
        self.assertEqual(_get_mode(self.token_path), 0o600)

    def test_migrate_from_json_secures_backup_file(self):
        """migrate_from_json must secure the .migrated backup file to 0600."""
        json_path = os.path.join(self.temp_dir.name, "calendar_db.json")
        with open(json_path, "w") as f:
            json.dump([{"title": "Test Event", "timestamp": "2026-11-08T10:00:00Z"}], f)
        os.chmod(json_path, 0o644)

        gc = MagicMock(spec=GoogleCalendar)
        gc.create_event.return_value = {"id": "gcal_1"}

        migrated = migrate_from_json(json_path, gc)
        self.assertEqual(migrated, 1)

        backup_path = json_path + ".migrated"
        self.assertTrue(os.path.exists(backup_path))
        self.assertEqual(_get_mode(backup_path), 0o600)


class TestEmailStorePermissions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "storage", "hozbot.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_init_db_creates_0600_db_and_0700_dir(self):
        """init_db must create hozbot.db with 0600 permissions and storage dir with 0700."""
        with patch.object(email_store, "DB_PATH", self.db_path):
            email_store.init_db()

            dir_path = os.path.dirname(self.db_path)
            self.assertTrue(os.path.isdir(dir_path))
            self.assertEqual(_get_mode(dir_path), 0o700)
            self.assertTrue(os.path.isfile(self.db_path))
            self.assertEqual(_get_mode(self.db_path), 0o600)

            # Also check wal/shm files if created
            for suffix in ("-wal", "-shm"):
                p = self.db_path + suffix
                if os.path.exists(p):
                    self.assertEqual(_get_mode(p), 0o600)

    def test_get_connection_creates_0600_db_even_without_prior_init(self):
        """_get_connection must ensure hozbot.db has 0600 permissions upon creation."""
        with patch.object(email_store, "DB_PATH", self.db_path):
            conn = email_store._get_connection()
            conn.close()

            self.assertTrue(os.path.isfile(self.db_path))
            self.assertEqual(_get_mode(self.db_path), 0o600)
            self.assertEqual(_get_mode(os.path.dirname(self.db_path)), 0o700)

    def test_email_store_permissions_under_permissive_umask(self):
        """init_db must enforce 0600 even when process umask is 0000."""
        old_umask = os.umask(0)
        try:
            with patch.object(email_store, "DB_PATH", self.db_path):
                email_store.init_db()
                self.assertEqual(_get_mode(self.db_path), 0o600)
                self.assertEqual(_get_mode(os.path.dirname(self.db_path)), 0o700)
        finally:
            os.umask(old_umask)


class TestMainDigestAndSkippedPermissions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.digest_path = os.path.join(self.temp_dir.name, "storage", "last_digest.json")
        self.skipped_path = os.path.join(self.temp_dir.name, "storage", "skipped_events.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_last_digest_time_creates_0600_file(self):
        """save_last_digest_time must create last_digest.json with 0600 permissions and dir with 0700."""
        with patch.object(main, "LAST_DIGEST_PATH", self.digest_path):
            main.save_last_digest_time("2026-11-07T19:00:00Z")

            self.assertTrue(os.path.isfile(self.digest_path))
            self.assertEqual(_get_mode(self.digest_path), 0o600)
            self.assertEqual(_get_mode(os.path.dirname(self.digest_path)), 0o700)

    def test_note_event_skipped_creates_0600_file(self):
        """note_event_skipped must create skipped_events.json with 0600 permissions and dir with 0700."""
        ctx = MagicMock()
        with patch.object(main, "SKIPPED_EVENTS_PATH", self.skipped_path):
            result = main.note_event_skipped(ctx, "Doctor Appointment", "2026-11-08")
            self.assertIn("Skipped event noted", result)

            self.assertTrue(os.path.isfile(self.skipped_path))
            self.assertEqual(_get_mode(self.skipped_path), 0o600)
            self.assertEqual(_get_mode(os.path.dirname(self.skipped_path)), 0o700)


class TestBinCollectionPermissions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_path = os.path.join(self.temp_dir.name, "storage", "bin_cache.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_cache_creates_0600_file_and_0700_dir(self):
        """_save_cache must create bin_cache.json with 0600 permissions and dir with 0700."""
        with patch.object(bin_collection, "CACHE_PATH", self.cache_path):
            bin_collection._save_cache("100080843318", [{"task": "refuse"}])

            self.assertTrue(os.path.isfile(self.cache_path))
            self.assertEqual(_get_mode(self.cache_path), 0o600)
            self.assertEqual(_get_mode(os.path.dirname(self.cache_path)), 0o700)


class TestLiveRepositoryState(unittest.TestCase):
    def test_live_storage_directory_permissions(self):
        """The actual project storage directory must have 0700 permissions."""
        storage_dir = os.path.join(main.BASE_DIR, "storage")
        if os.path.exists(storage_dir):
            self.assertEqual(
                _get_mode(storage_dir),
                0o700,
                f"Storage directory {storage_dir} must be restricted to 0700",
            )

    def test_live_storage_files_permissions(self):
        """All existing files in project storage directory must have 0600 permissions."""
        storage_dir = os.path.join(main.BASE_DIR, "storage")
        if os.path.exists(storage_dir):
            for entry in os.listdir(storage_dir):
                entry_path = os.path.join(storage_dir, entry)
                if os.path.isfile(entry_path):
                    self.assertEqual(
                        _get_mode(entry_path),
                        0o600,
                        f"Storage file {entry_path} must be restricted to 0600",
                    )

    def test_live_env_file_permissions(self):
        """The actual project .env file (if present) must have 0600 permissions."""
        env_file = os.path.join(main.BASE_DIR, ".env")
        if os.path.exists(env_file):
            self.assertEqual(
                _get_mode(env_file),
                0o600,
                f".env file {env_file} must be restricted to 0600",
            )


if __name__ == "__main__":
    unittest.main()
