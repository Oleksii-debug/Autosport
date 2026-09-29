from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_source_checkout


class WindowsBuildGitContextTests(unittest.TestCase):
    @staticmethod
    def _run_git(root: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    @staticmethod
    def _clean_repo(root: Path) -> str:
        WindowsBuildGitContextTests._run_git(root, "init")
        WindowsBuildGitContextTests._run_git(root, "config", "user.email", "autosport-tests@example.invalid")
        WindowsBuildGitContextTests._run_git(root, "config", "user.name", "Autosport Tests")
        (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
        (root / "tracked.py").write_bytes(b"VALUE = 1\n")
        WindowsBuildGitContextTests._run_git(root, "add", ".gitattributes", "tracked.py")
        WindowsBuildGitContextTests._run_git(root, "commit", "-m", "fixture")
        return WindowsBuildGitContextTests._run_git(root, "rev-parse", "HEAD")

    @staticmethod
    def _without_github_event_environment():
        return patch.dict(
            os.environ,
            {"GITHUB_EVENT_PATH": "", "GITHUB_REPOSITORY": "", "GITHUB_SHA": ""},
            clear=False,
        )

    def test_git_environment_strips_repository_shaping_overrides(self) -> None:
        injected = {
            "GIT_WORK_TREE": "redirected-worktree",
            "GIT_DIR": "redirected-git-dir",
            "GIT_INDEX_FILE": "redirected-index",
            "GIT_COMMON_DIR": "redirected-common-dir",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.worktree",
            "GIT_CONFIG_VALUE_0": "redirected-config-worktree",
            "GIT_NO_REPLACE_OBJECTS": "0",
        }
        with patch.dict(os.environ, injected, clear=False):
            sanitized = verify_source_checkout._git_environment()

        for name in injected:
            if name != "GIT_NO_REPLACE_OBJECTS":
                self.assertNotIn(name, sanitized)
        self.assertEqual(sanitized["GIT_NO_REPLACE_OBJECTS"], "1")

    def test_verifier_rejects_untracked_input_despite_inherited_worktree_redirect(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as redirected_dir:
            root = Path(repo_dir)
            redirected = Path(redirected_dir)
            source_sha = self._clean_repo(root)
            (root / "evil.py").write_bytes(b"EVIL = 1\n")

            with patch.dict(os.environ, {"GIT_WORK_TREE": str(redirected)}, clear=False):
                self.assertEqual(self._run_git(root, "ls-files", "--others", "--exclude-standard"), "")
                with self._without_github_event_environment():
                    with self.assertRaisesRegex(ValueError, "checkout is not pristine"):
                        verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)

    def test_verifier_requires_exact_git_toplevel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            nested = root / "nested"
            nested.mkdir()

            with self._without_github_event_environment():
                with self.assertRaisesRegex(ValueError, "top-level worktree does not match repo_root"):
                    verify_source_checkout.verify_source_checkout(source_sha, repo_root=nested)


if __name__ == "__main__":
    unittest.main()
