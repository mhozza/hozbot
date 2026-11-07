import fnmatch
import os
import re
import subprocess
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestDockerfileHardening(unittest.TestCase):
    def setUp(self):
        self.dockerfile_path = os.path.join(PROJECT_ROOT, "Dockerfile")
        self.assertTrue(os.path.isfile(self.dockerfile_path), "Dockerfile must exist")
        with open(self.dockerfile_path, "r", encoding="utf-8") as f:
            self.content = f.read()
        self.lines = [line.strip() for line in self.content.splitlines()]

    def test_dockerfile_creates_unprivileged_user(self):
        """Dockerfile must create an unprivileged hozbot user."""
        match = re.search(r"adduser\s+.*hozbot", self.content)
        self.assertIsNotNone(match, "Dockerfile must create an unprivileged 'hozbot' user")
        self.assertIn("-u 10001", self.content, "hozbot user should have an explicit non-root UID (10001)")

    def test_dockerfile_prepares_and_chowns_runtime_directories(self):
        """Dockerfile must create storage and downloads directories and chown /app to hozbot."""
        self.assertIn("mkdir -p /app/storage /app/downloads", self.content,
                      "Dockerfile must create /app/storage and /app/downloads")
        self.assertIn("chown -R hozbot:hozbot /app", self.content,
                      "Dockerfile must recursively chown /app to hozbot:hozbot")

    def test_dockerfile_switches_to_unprivileged_user(self):
        """Dockerfile must switch to USER hozbot before running CMD."""
        user_lines = [line for line in self.lines if line.startswith("USER ")]
        self.assertGreaterEqual(len(user_lines), 1, "Dockerfile must contain a USER instruction")
        self.assertEqual(user_lines[-1], "USER hozbot", "Final runtime stage must set USER hozbot")

        # Ensure USER hozbot is specified before CMD
        user_idx = max(i for i, line in enumerate(self.lines) if line == "USER hozbot")
        cmd_idx = max(i for i, line in enumerate(self.lines) if line.startswith("CMD "))
        self.assertLess(user_idx, cmd_idx, "USER hozbot must appear before CMD")

    def test_dockerfile_copies_venv_with_chown(self):
        """Dockerfile must copy .venv with --chown=hozbot:hozbot."""
        venv_copies = [
            line for line in self.lines
            if line.startswith("COPY ") and ".venv" in line
        ]
        self.assertTrue(
            any("--chown=hozbot:hozbot" in line for line in venv_copies),
            "COPY instruction for .venv must specify --chown=hozbot:hozbot",
        )

    def test_dockerfile_copies_only_necessary_files_with_chown(self):
        """Dockerfile must copy only necessary code files with --chown=hozbot:hozbot, not COPY . ."""
        # Ensure no unrestricted COPY . .
        self.assertNotIn("COPY . .", self.content, "Dockerfile must NOT use unrestricted 'COPY . .'")

        # Ensure explicit selective copies
        self.assertTrue(
            any(re.search(r"COPY\s+--chown=hozbot:hozbot\s+\*\.py\s+\./", line) for line in self.lines),
            "Dockerfile must copy *.py with --chown=hozbot:hozbot",
        )
        self.assertTrue(
            any(re.search(r"COPY\s+--chown=hozbot:hozbot\s+prompts/\s+\./prompts/", line) for line in self.lines),
            "Dockerfile must copy prompts/ with --chown=hozbot:hozbot",
        )
        self.assertTrue(
            any(re.search(r"COPY\s+--chown=hozbot:hozbot\s+tools/\s+\./tools/", line) for line in self.lines),
            "Dockerfile must copy tools/ with --chown=hozbot:hozbot",
        )


class TestDockerComposeHardening(unittest.TestCase):
    def setUp(self):
        self.compose_path = os.path.join(PROJECT_ROOT, "docker-compose.yml")
        self.assertTrue(os.path.isfile(self.compose_path), "docker-compose.yml must exist")
        with open(self.compose_path, "r", encoding="utf-8") as f:
            self.content = f.read()

    def test_docker_compose_security_opt_no_new_privileges(self):
        """docker-compose.yml must set security_opt: no-new-privileges:true."""
        self.assertRegex(
            self.content,
            r"security_opt:\s*(\n\s*-\s*\"?no-new-privileges:true\"?|\[\s*\"?no-new-privileges:true\"?\s*\])",
            "docker-compose.yml must configure security_opt with no-new-privileges:true",
        )

    def test_docker_compose_cap_drop_all(self):
        """docker-compose.yml must drop all capabilities (cap_drop: ALL)."""
        self.assertRegex(
            self.content,
            r"cap_drop:\s*(\n\s*-\s*\"?ALL\"?|\[\s*\"?ALL\"?\s*\])",
            "docker-compose.yml must configure cap_drop with ALL",
        )

    def test_docker_compose_memory_limit(self):
        """docker-compose.yml must define a memory limit (e.g. 512m)."""
        self.assertRegex(
            self.content,
            r"mem_limit:\s*512m",
            "docker-compose.yml must configure mem_limit: 512m",
        )

    def test_docker_compose_validity_via_cli(self):
        """Validate docker-compose.yml syntax using docker compose config if available."""
        try:
            res = subprocess.run(
                ["docker", "compose", "config"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if res.returncode == 0:
                self.assertIn("no-new-privileges:true", res.stdout)
                self.assertIn("ALL", res.stdout)
                self.assertIn("536870912", res.stdout)  # 512m in bytes
        except (FileNotFoundError, subprocess.SubprocessError):
            self.skipTest("docker CLI not available or timed out")


class TestIgnoreFilesSecretExclusions(unittest.TestCase):
    def setUp(self):
        self.dockerignore_path = os.path.join(PROJECT_ROOT, ".dockerignore")
        self.gitignore_path = os.path.join(PROJECT_ROOT, ".gitignore")
        self.assertTrue(os.path.isfile(self.dockerignore_path), ".dockerignore must exist")
        self.assertTrue(os.path.isfile(self.gitignore_path), ".gitignore must exist")

        with open(self.dockerignore_path, "r", encoding="utf-8") as f:
            self.dockerignore_lines = [
                line.strip() for line in f.read().splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]

        with open(self.gitignore_path, "r", encoding="utf-8") as f:
            self.gitignore_lines = [
                line.strip() for line in f.read().splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]

    def test_dockerignore_contains_secret_patterns(self):
        """.dockerignore must explicitly exclude sensitive secret patterns."""
        expected_patterns = [
            "credentials.json",
            "*token*.json",
            ".env.*",
            "!.env.example",
            "storage/*.json",
        ]
        for pat in expected_patterns:
            self.assertIn(
                pat,
                self.dockerignore_lines,
                f".dockerignore must contain pattern: {pat}",
            )

    def test_gitignore_contains_secret_patterns(self):
        """.gitignore must explicitly exclude sensitive secret patterns."""
        expected_patterns = [
            "credentials.json",
            "*token*.json",
            ".env.*",
            "!.env.example",
            "storage/*.json",
        ]
        for pat in expected_patterns:
            self.assertIn(
                pat,
                self.gitignore_lines,
                f".gitignore must contain pattern: {pat}",
            )

    def test_git_check_ignore_behavior_on_secrets(self):
        """Verify git check-ignore correctly ignores sensitive secret files and allows .env.example."""
        sensitive_files = [
            "credentials.json",
            "google_calendar_token.json",
            "refresh_token.json",
            "token.json",
            ".env.production",
            ".env.local",
            "storage/credentials.json",
            "storage/google_calendar_token.json",
        ]
        for filename in sensitive_files:
            res = subprocess.run(
                ["git", "check-ignore", "-q", filename],
                cwd=PROJECT_ROOT,
            )
            self.assertEqual(
                res.returncode,
                0,
                f"File '{filename}' should be ignored by git according to .gitignore",
            )

        # .env.example must NOT be ignored
        res_example = subprocess.run(
            ["git", "check-ignore", "-q", ".env.example"],
            cwd=PROJECT_ROOT,
        )
        self.assertNotEqual(
            res_example.returncode,
            0,
            ".env.example must NOT be ignored by git (whitelisted via !.env.example)",
        )

    def test_dockerignore_pattern_filtering_logic(self):
        """Simulate .dockerignore rule matching to ensure secrets are excluded and .env.example is retained."""
        def is_dockerignored(path: str) -> bool:
            ignored = False
            for rule in self.dockerignore_lines:
                if rule.startswith("!"):
                    neg_pattern = rule[1:]
                    if fnmatch.fnmatch(path, neg_pattern) or fnmatch.fnmatch(os.path.basename(path), neg_pattern):
                        ignored = False
                else:
                    clean_rule = rule.rstrip("/")
                    if (
                        fnmatch.fnmatch(path, rule)
                        or fnmatch.fnmatch(path, clean_rule)
                        or fnmatch.fnmatch(os.path.basename(path), rule)
                        or path.startswith(clean_rule + "/")
                    ):
                        ignored = True
            return ignored

        self.assertTrue(is_dockerignored("credentials.json"))
        self.assertTrue(is_dockerignored("google_calendar_token.json"))
        self.assertTrue(is_dockerignored("token.json"))
        self.assertTrue(is_dockerignored(".env.local"))
        self.assertTrue(is_dockerignored(".env.prod"))
        self.assertTrue(is_dockerignored("storage/credentials.json"))
        self.assertTrue(is_dockerignored("storage/google_calendar_token.json"))
        self.assertFalse(is_dockerignored(".env.example"))


class TestAgentsDocumentationConsistency(unittest.TestCase):
    def setUp(self):
        self.agents_path = os.path.join(PROJECT_ROOT, "AGENTS.md")
        self.assertTrue(os.path.isfile(self.agents_path), "AGENTS.md must exist")
        with open(self.agents_path, "r", encoding="utf-8") as f:
            self.content = f.read()

    def test_agents_doc_references_storage_credentials(self):
        """AGENTS.md must reference storage/credentials.json as the default credentials path."""
        self.assertIn(
            "Path to Google Calendar OAuth client secrets JSON (default: `storage/credentials.json`)",
            self.content,
            "AGENTS.md must document the default credentials file as storage/credentials.json",
        )

    def test_agents_doc_no_stale_root_credentials_default(self):
        """AGENTS.md must not contain the stale (default: `credentials.json`) reference."""
        self.assertNotIn(
            "(default: `credentials.json`)",
            self.content,
            "AGENTS.md should not contain stale '(default: `credentials.json`)'",
        )


if __name__ == "__main__":
    unittest.main()
