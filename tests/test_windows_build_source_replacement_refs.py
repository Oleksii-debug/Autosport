from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_source_checkout


class WindowsBuildReplacementRefTests(unittest.TestCase):
    @staticmethod
    def _run_git(root: Path, *args: str) -> str:
        # The hardened Windows build intentionally exports GIT_NO_REPLACE_OBJECTS=1
        # before running pytest.  This helper is the test fixture's *ordinary Git*
        # oracle, so it must not inherit that production hardening switch; otherwise
        # the positive control cannot observe the replacement ref it just created.
        env = os.environ.copy()
        env.pop("GIT_NO_REPLACE_OBJECTS", None)
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            env=env,
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

    def _replacement_repo(self, root: Path) -> tuple[str, str]:
        self._run_git(root, "init")
        self._run_git(root, "config", "user.email", "autosport-tests@example.invalid")
        self._run_git(root, "config", "user.name", "Autosport Tests")
        (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
        (root / "tracked.py").write_bytes(b"VALUE = 1\n")
        self._run_git(root, "add", ".gitattributes", "tracked.py")
        self._run_git(root, "commit", "-m", "source-a")
        source_a = self._run_git(root, "rev-parse", "HEAD")

        (root / "tracked.py").write_bytes(b"VALUE = 2\n")
        self._run_git(root, "add", "tracked.py")
        self._run_git(root, "commit", "-m", "source-b")
        source_b = self._run_git(root, "rev-parse", "HEAD")

        self._run_git(root, "reset", "--hard", source_a)
        self._run_git(root, "replace", source_a, source_b)
        self._run_git(root, "read-tree", source_b)
        self._run_git(root, "checkout-index", "-a", "-f")
        return source_a, source_b

    def test_git_source_queries_ignore_replacement_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_a, source_b = self._replacement_repo(root)

            # Use cat-file as the portable replacement-object oracle. Git documents
            # this command as returning replacement commit bytes by default, while
            # --no-replace-objects (and GIT_NO_REPLACE_OBJECTS) returns the original.
            ordinary_commit = self._run_git(root, "cat-file", "commit", source_a)
            source_b_commit = self._run_git(
                root,
                "--no-replace-objects",
                "cat-file",
                "commit",
                source_b,
            )
            self.assertEqual(ordinary_commit, source_b_commit)

            protected_commit = verify_source_checkout._git_output(
                root,
                "cat-file",
                "commit",
                source_a,
            )
            source_a_commit = self._run_git(
                root,
                "--no-replace-objects",
                "cat-file",
                "commit",
                source_a,
            )
            self.assertEqual(protected_commit, source_a_commit)

            entries = verify_source_checkout._source_tree_entries(root, source_a)
            source_a_blob = verify_source_checkout._git_blob_sha1(b"VALUE = 1\n").encode("ascii")
            self.assertEqual(entries[b"tracked.py"][1], source_a_blob)

    def test_verifier_rejects_repository_with_replacement_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_a, _source_b = self._replacement_repo(root)

            with self._without_github_event_environment():
                with self.assertRaisesRegex(ValueError, "Git replacement refs"):
                    verify_source_checkout.verify_source_checkout(source_a, repo_root=root)


if __name__ == "__main__":
    unittest.main()
