from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_source_checkout


class WindowsBuildIndexMaskingTests(unittest.TestCase):
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
    def _without_github_event_environment():
        return patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_PATH": "",
                "GITHUB_REPOSITORY": "",
                "GITHUB_SHA": "",
            },
            clear=False,
        )

    def _clean_repo(self, root: Path) -> str:
        self._run_git(root, "init")
        self._run_git(root, "config", "user.email", "autosport-tests@example.invalid")
        self._run_git(root, "config", "user.name", "Autosport Tests")
        (root / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
        self._run_git(root, "add", "tracked.py")
        self._run_git(root, "commit", "-m", "fixture")
        return self._run_git(root, "rev-parse", "HEAD")

    def test_preflight_rejects_assume_unchanged_masked_tracked_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            self._run_git(root, "update-index", "--assume-unchanged", "tracked.py")
            (root / "tracked.py").write_text("VALUE = 999\n", encoding="utf-8")

            # Reproduce the source-review bypass: ordinary status is clean even though
            # tracked working-tree bytes no longer match the exact commit.
            self.assertEqual(
                self._run_git(root, "status", "--porcelain=v1", "--untracked-files=all"),
                "",
            )

            with self._without_github_event_environment():
                with self.assertRaisesRegex(ValueError, "index-masked"):
                    verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)

    def test_late_boundary_rejects_skip_worktree_masked_tracked_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)

            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)

                self._run_git(root, "update-index", "--skip-worktree", "tracked.py")
                (root / "tracked.py").write_text("VALUE = 999\n", encoding="utf-8")

                # skip-worktree can hide the same post-preflight drift from status.
                self.assertEqual(
                    self._run_git(
                        root,
                        "status",
                        "--porcelain=v1",
                        "--untracked-files=all",
                    ),
                    "",
                )

                with self.assertRaisesRegex(ValueError, "index-masked"):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )


if __name__ == "__main__":
    unittest.main()
