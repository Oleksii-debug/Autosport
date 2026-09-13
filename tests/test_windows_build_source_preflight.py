from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_source_checkout


class WindowsBuildSourcePreflightTests(unittest.TestCase):
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
        # Keep PATH/SystemRoot intact so Windows can still resolve git.exe.
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
        (root / ".gitignore").write_text("*.ignored.py\n", encoding="utf-8")
        (root / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
        self._run_git(root, "add", ".gitignore", "tracked.py")
        self._run_git(root, "commit", "-m", "fixture")
        return self._run_git(root, "rev-parse", "HEAD")

    def test_clean_exact_checkout_passes_prebuild_source_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)

    def test_modified_tracked_build_input_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            (root / "tracked.py").write_text("VALUE = 999\n", encoding="utf-8")
            with self._without_github_event_environment():
                with self.assertRaisesRegex(ValueError, "not pristine before"):
                    verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)

    def test_untracked_import_shadowing_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            (root / "autosport.py").write_text("raise RuntimeError('shadow')\n", encoding="utf-8")
            with self._without_github_event_environment():
                with self.assertRaisesRegex(ValueError, "not pristine before"):
                    verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)

    def test_ignored_import_shadowing_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            (root / "malicious.ignored.py").write_text("raise RuntimeError('shadow')\n", encoding="utf-8")
            with self._without_github_event_environment():
                with self.assertRaisesRegex(ValueError, "not pristine before"):
                    verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)

    def test_windows_workflow_checks_out_and_preflights_exact_candidate_before_build(self) -> None:
        workflow = Path(".github/workflows/windows-build.yml").read_text(encoding="utf-8")
        exact_ref = "ref: ${{ github.event.pull_request.head.sha || github.sha }}"
        preflight = "python scripts/verify_source_checkout.py --source-sha $env:AUTOSPORT_SOURCE_SHA"
        build = "run: ./scripts/build_windows.ps1"
        self.assertIn(exact_ref, workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn(preflight, workflow)
        self.assertLess(workflow.index(preflight), workflow.index(build))

    def test_ci_workflow_qualifies_exact_candidate_not_synthetic_merge_ref(self) -> None:
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        exact_ref = "ref: ${{ github.event.pull_request.head.sha || github.sha }}"
        unix_preflight = (
            'python scripts/verify_source_checkout.py --source-sha "$AUTOSPORT_SOURCE_SHA"'
        )
        windows_preflight = (
            "python scripts/verify_source_checkout.py --source-sha $env:AUTOSPORT_SOURCE_SHA"
        )
        install = "python -m pip install -e '.[test]'"
        self.assertIn("AUTOSPORT_SOURCE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}", workflow)
        self.assertIn(exact_ref, workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn(unix_preflight, workflow)
        self.assertIn(windows_preflight, workflow)
        self.assertLess(workflow.index(unix_preflight), workflow.index(install))
        self.assertLess(workflow.index(windows_preflight), workflow.index(install))


if __name__ == "__main__":
    unittest.main()
