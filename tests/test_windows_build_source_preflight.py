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
        (root / ".gitignore").write_text(
            "*.ignored.py\n.env\n__pycache__/\n.pytest_cache/\n*.egg-info/\nbuild/\ndist/\n*.spec\n",
            encoding="utf-8",
        )
        (root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "autosport-lab"\n'
            'version = "0.1.0"\n'
            "\n"
            "[project.scripts]\n"
            'fixture-tool = "fixture.cli:main"\n',
            encoding="utf-8",
        )
        (root / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "WINDOWS_START_HERE.txt").write_text("canonical start file\n", encoding="utf-8")
        self._run_git(
            root,
            "add",
            ".gitignore",
            "pyproject.toml",
            "tracked.py",
            "WINDOWS_START_HERE.txt",
        )
        self._run_git(root, "commit", "-m", "fixture")
        return self._run_git(root, "rev-parse", "HEAD")

    @staticmethod
    def _write_valid_egg_info(root: Path) -> Path:
        egg_info = root / "src" / "autosport_lab.egg-info"
        egg_info.mkdir(parents=True)
        (egg_info / "PKG-INFO").write_text("generated\n", encoding="utf-8")
        (egg_info / "dependency_links.txt").write_text("\n", encoding="utf-8")
        (egg_info / "entry_points.txt").write_text(
            "[console_scripts]\nfixture-tool = fixture.cli:main\n",
            encoding="utf-8",
        )
        (egg_info / "requires.txt").write_text("\n", encoding="utf-8")
        (egg_info / "SOURCES.txt").write_text("tracked.py\n", encoding="utf-8")
        (egg_info / "top_level.txt").write_text("fixture\n", encoding="utf-8")
        return egg_info

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

    def test_late_boundary_rejects_tracked_mutation_after_clean_initial_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)
                (root / "tracked.py").write_text("VALUE = 999\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "changed after initial preflight"):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )

    def test_late_boundary_rejects_staged_index_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)
                (root / "tracked.py").write_text("VALUE = 2\n", encoding="utf-8")
                self._run_git(root, "add", "tracked.py")
                with self.assertRaisesRegex(ValueError, "changed after initial preflight"):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )

    def test_late_boundary_allows_only_expected_non_source_build_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)

                pytest_cache = root / ".pytest_cache"
                pytest_cache.mkdir()
                (pytest_cache / "README.md").write_text("generated\n", encoding="utf-8")
                self._write_valid_egg_info(root)

                build = root / "build" / "Autosport"
                build.mkdir(parents=True)
                (build / "Analysis-00.toc").write_text("generated\n", encoding="utf-8")
                dist = root / "dist"
                dist.mkdir()
                (dist / "Autosport.exe").write_bytes(b"generated executable")
                (root / "Autosport.spec").write_text("generated\n", encoding="utf-8")

                verify_source_checkout.verify_source_checkout(
                    source_sha,
                    repo_root=root,
                    late_build_boundary=True,
                )

    def test_repeated_late_boundary_rejects_package_input_mutation_after_first_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)
                self._write_valid_egg_info(root)

                # This models the first gate passing immediately before Autosport.exe.
                verify_source_checkout.verify_source_checkout(
                    source_sha,
                    repo_root=root,
                    late_build_boundary=True,
                )

                # First-PyInstaller products are expected generated outputs, but a tracked package
                # input changed afterwards must be stopped by the next gate.
                build = root / "build" / "Autosport"
                build.mkdir(parents=True)
                (build / "Analysis-00.toc").write_text("generated\n", encoding="utf-8")
                dist = root / "dist"
                dist.mkdir()
                (dist / "Autosport.exe").write_bytes(b"generated executable")
                (root / "Autosport.spec").write_text("generated\n", encoding="utf-8")
                (root / "WINDOWS_START_HERE.txt").write_text(
                    "mutated after first build\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, "changed after initial preflight"):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )

    def test_late_boundary_rejects_source_adjacent_bytecode_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)
                cache = root / "__pycache__"
                cache.mkdir()
                (cache / "tracked.cpython-312.pyc").write_bytes(b"unchecked build input")

                with self.assertRaisesRegex(ValueError, "changed after initial preflight"):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )

    def test_late_boundary_rejects_pyinstaller_hook_entry_point_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)
                egg_info = self._write_valid_egg_info(root)
                (egg_info / "entry_points.txt").write_text(
                    "[console_scripts]\n"
                    "fixture-tool = fixture.cli:main\n"
                    "\n"
                    "[pyinstaller40]\n"
                    "hook-dirs = hostile.hooks:get_hook_dirs\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "entry_points.txt does not match tracked pyproject.toml",
                ):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )

    def test_late_boundary_rejects_unknown_egg_info_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)
                egg_info = self._write_valid_egg_info(root)
                (egg_info / "unexpected-hook.py").write_text(
                    "raise RuntimeError('unexpected build input')\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, "changed after initial preflight"):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )

    def test_late_boundary_rejects_unexpected_ignored_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)
                (root / ".env").write_text("BUILD_OVERRIDE=unsafe\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "changed after initial preflight"):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )

    def test_late_boundary_still_rejects_nonignored_untracked_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)
                (root / "late_shadow.py").write_text("raise RuntimeError('shadow')\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "changed after initial preflight"):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )

    def test_windows_workflow_checks_out_and_preflights_exact_candidate_before_build(self) -> None:
        workflow = Path(".github/workflows/windows-build.yml").read_text(encoding="utf-8")
        exact_ref = "ref: ${{ github.event.pull_request.head.sha || github.sha }}"
        preflight = "python scripts/verify_source_checkout.py --source-sha $env:AUTOSPORT_SOURCE_SHA"
        build = "run: ./scripts/build_windows.ps1"
        self.assertIn(exact_ref, workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn(preflight, workflow)
        self.assertLess(workflow.index(preflight), workflow.index(build))


if __name__ == "__main__":
    unittest.main()
