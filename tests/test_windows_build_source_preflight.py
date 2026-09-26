from __future__ import annotations

import os
import subprocess
import sys
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
        # Mirror the release checkout's byte-canonical text policy and write the
        # fixture bytes explicitly so Windows newline translation cannot make
        # the fixture itself violate the raw worktree-vs-blob invariant.
        (root / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
        (root / ".gitignore").write_bytes(
            b"*.ignored.py\n.env\n__pycache__/\n.pytest_cache/\n*.egg-info/\nbuild/\ndist/\n*.spec\n"
        )
        (root / "tracked.py").write_bytes(b"VALUE = 1\n")
        (root / "WINDOWS_START_HERE.txt").write_bytes(b"start\n")
        (root / "pyproject.toml").write_bytes(
            b"[project]\n"
            b"name = \"autosport-lab\"\n"
            b"[project.scripts]\n"
            b"autosport = \"autosport.cli:main\"\n"
        )
        self._run_git(
            root,
            "add",
            ".gitattributes",
            ".gitignore",
            "tracked.py",
            "WINDOWS_START_HERE.txt",
            "pyproject.toml",
        )
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
                with self.assertRaisesRegex(ValueError, "raw tracked bytes"):
                    verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)

    def test_raw_tracked_bytes_reject_clean_filter_index_masking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            attributes = root / ".git" / "info" / "attributes"
            attributes.write_text("tracked.py filter=mask\n", encoding="utf-8")
            self._run_git(root, "config", "filter.mask.clean", "git show HEAD:tracked.py")
            self._run_git(root, "config", "filter.mask.smudge", "cat")
            (root / "tracked.py").write_text("VALUE = 999\n", encoding="utf-8")
            self._run_git(root, "add", "tracked.py")

            # The mutable clean filter plus refreshed index stat makes ordinary
            # status look clean even though the raw executable source bytes differ.
            self.assertEqual(
                self._run_git(root, "status", "--porcelain=v1", "--untracked-files=all"),
                "",
            )
            with self._without_github_event_environment():
                with self.assertRaisesRegex(ValueError, "raw tracked bytes"):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )

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
                with self.assertRaisesRegex(ValueError, "raw tracked bytes"):
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
                with self.assertRaisesRegex(ValueError, "raw tracked bytes"):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )

    def test_first_late_boundary_rejects_premature_release_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)
                build = root / "build" / "Autosport"
                build.mkdir(parents=True)
                (build / "analysis.toc").write_text("premature\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, r"ignored:build/Autosport/analysis\.toc"):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )

    def test_later_boundary_allows_only_non_source_release_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)
                pytest_cache = root / ".pytest_cache"
                pytest_cache.mkdir()
                (pytest_cache / "README.md").write_text("generated\n", encoding="utf-8")
                build = root / "build" / "Autosport"
                build.mkdir(parents=True)
                (build / "analysis.toc").write_text("generated\n", encoding="utf-8")
                dist = root / "dist"
                dist.mkdir()
                (dist / "Autosport.exe").write_bytes(b"generated executable")
                (root / "Autosport.spec").write_text("generated\n", encoding="utf-8")
                verify_source_checkout.verify_source_checkout(
                    source_sha,
                    repo_root=root,
                    late_build_boundary=True,
                    allow_release_outputs=True,
                )

    def test_late_boundary_rejects_executable_bytecode_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)
                cache = root / "__pycache__"
                cache.mkdir()
                (cache / "tracked.cpython-312.pyc").write_bytes(b"unchecked executable bytecode")
                with self.assertRaisesRegex(ValueError, r"ignored:__pycache__/"):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )

    def test_late_boundary_rejects_editable_entry_point_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)
                egg_info = root / "src" / "autosport_lab.egg-info"
                egg_info.mkdir(parents=True)
                (egg_info / "entry_points.txt").write_text(
                    "[pyinstaller40]\nhook-dirs = hostile:hook_dirs\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ValueError,
                    r"ignored:src/autosport_lab\.egg-info/entry_points\.txt",
                ):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )

    def test_cli_late_boundary_rejects_bytecode_cache_after_valid_egg_info_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            cache = root / "src" / "autosport" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "module.pyc").write_bytes(b"generated bytecode")
            egg_info = root / "src" / "autosport_lab.egg-info"
            egg_info.mkdir(parents=True)
            (egg_info / "entry_points.txt").write_text(
                "[console_scripts]\nautosport = autosport.cli:main\n",
                encoding="utf-8",
            )

            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                with self._without_github_event_environment(), patch.object(
                    sys,
                    "argv",
                    [
                        "verify_source_checkout.py",
                        "--source-sha",
                        source_sha,
                        "--late-build-boundary",
                    ],
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        r"ignored:src/autosport/__pycache__/",
                    ):
                        verify_source_checkout.main()
            finally:
                os.chdir(old_cwd)

            self.assertTrue(cache.exists())
            self.assertFalse(egg_info.exists())

    def test_cli_late_boundary_rejects_hostile_editable_entry_point_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            egg_info = root / "src" / "autosport_lab.egg-info"
            egg_info.mkdir(parents=True)
            hostile = egg_info / "entry_points.txt"
            hostile.write_text(
                "[pyinstaller40]\nhook-dirs = hostile:hook_dirs\n",
                encoding="utf-8",
            )

            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                with self._without_github_event_environment(), patch.object(
                    sys,
                    "argv",
                    [
                        "verify_source_checkout.py",
                        "--source-sha",
                        source_sha,
                        "--late-build-boundary",
                    ],
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "entry_points.txt does not match tracked pyproject.toml",
                    ):
                        verify_source_checkout.main()
            finally:
                os.chdir(old_cwd)

            self.assertTrue(hostile.exists())

    def test_generated_build_input_cleanup_removes_only_editable_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)
                cache = root / "src" / "autosport" / "__pycache__"
                cache.mkdir(parents=True)
                (cache / "module.pyc").write_bytes(b"generated bytecode")
                egg_info = root / "src" / "autosport_lab.egg-info"
                egg_info.mkdir(parents=True)
                (egg_info / "entry_points.txt").write_text(
                    "[console_scripts]\nautosport = autosport.cli:main\n",
                    encoding="utf-8",
                )
                verify_source_checkout.clean_late_generated_build_inputs(root)
                self.assertTrue(cache.exists())
                self.assertFalse(egg_info.exists())
                with self.assertRaisesRegex(
                    ValueError,
                    r"ignored:src/autosport/__pycache__/",
                ):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )

    def test_later_boundary_rejects_post_first_build_tracked_package_input_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._clean_repo(root)
            with self._without_github_event_environment():
                verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)
                dist = root / "dist"
                dist.mkdir()
                (dist / "Autosport.exe").write_bytes(b"first build output")
                verify_source_checkout.verify_source_checkout(
                    source_sha,
                    repo_root=root,
                    late_build_boundary=True,
                    allow_release_outputs=True,
                )
                (root / "WINDOWS_START_HERE.txt").write_text(
                    "mutated after first build\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "raw tracked bytes"):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                        allow_release_outputs=True,
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

    def test_bound_release_artifact_detects_replacement_and_decouples_live_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            live = root / "dist" / "Autosport.exe"
            bound = root / "bound" / "Autosport.exe"
            live.parent.mkdir(parents=True)
            live.write_bytes(b"pyinstaller output")

            digest = verify_source_checkout.bind_release_artifact(live, bound)
            live.write_bytes(b"replaced live output")
            verify_source_checkout.require_artifact_sha256(bound, digest)
            self.assertEqual(bound.read_bytes(), b"pyinstaller output")

            bound.write_bytes(b"tampered bound output")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                verify_source_checkout.require_artifact_sha256(bound, digest)

    def test_windows_build_reproves_trusted_snapshot_before_each_later_source_consumer(self) -> None:
        script = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
        snapshot_gate = "python $sourceVerifier --source-sha $sourceSha --late-build-boundary"
        release_output_gate = snapshot_gate + " --allow-release-outputs"
        live_late_gate = "python scripts/verify_source_checkout.py --source-sha $sourceSha --late-build-boundary"
        locked_snapshot_gate = (
            "$trustedBuildManifestJson | & $pythonExecutable -I -S -c "
            "$trustedSourceSnapshotVerifierLauncher $trustedBuildRoot"
        )
        first_build = (
            "& $packagingPython -I -m PyInstaller --noconfirm --clean --onefile --windowed "
            "--paths $trustedBuildSrc --add-data $trustedWebAssetsSpec --distpath $pyInstallerDist --workpath $pyInstallerWork "
            "--specpath $pyInstallerSpec --name Autosport $trustedGuiEntry"
        )
        second_build = (
            "& $packagingPython -I -m PyInstaller --noconfirm --clean --onefile --console "
            "--paths $trustedBuildSrc --distpath $pyInstallerDist --workpath $pyInstallerWork "
            "--specpath $pyInstallerSpec --name Autosport-Data $trustedDataEntry"
        )
        package_build = "python scripts/package_windows.py `"

        first_gate = script.index(snapshot_gate)
        first_locked_gate = script.index(locked_snapshot_gate, first_gate)
        first_build_index = script.index(first_build)
        post_first_locked_gate = script.index(locked_snapshot_gate, first_build_index)
        second_gate = script.index(release_output_gate, first_build_index)
        pre_second_locked_gate = script.index(locked_snapshot_gate, second_gate)
        second_build_index = script.index(second_build)
        post_second_locked_gate = script.index(locked_snapshot_gate, second_build_index)
        package_gate = script.index(release_output_gate, second_build_index)
        package_build_index = script.index(package_build)

        self.assertNotIn(live_late_gate, script)
        self.assertEqual(script.count(snapshot_gate), 3)
        self.assertEqual(script.count(release_output_gate), 2)
        self.assertEqual(script.count(locked_snapshot_gate), 4)
        self.assertLess(first_gate, first_locked_gate)
        self.assertLess(first_locked_gate, first_build_index)
        self.assertLess(first_build_index, post_first_locked_gate)
        self.assertLess(post_first_locked_gate, second_gate)
        self.assertLess(second_gate, pre_second_locked_gate)
        self.assertLess(pre_second_locked_gate, second_build_index)
        self.assertLess(second_build_index, post_second_locked_gate)
        self.assertLess(post_second_locked_gate, package_gate)
        self.assertLess(package_gate, package_build_index)
        self.assertIn(
            "Copy-Item -LiteralPath 'scripts/verify_source_checkout.py' -Destination $sourceVerifier -Force",
            script,
        )
        self.assertIn("$env:PYTHONDONTWRITEBYTECODE = '1'", script)

    def test_windows_build_binds_pyinstaller_outputs_before_audit_and_package(self) -> None:
        script = Path("scripts/build_windows.ps1").read_text(encoding="utf-8")
        first_build = (
            "& $packagingPython -I -m PyInstaller --noconfirm --clean --onefile --windowed "
            "--paths $trustedBuildSrc --add-data $trustedWebAssetsSpec --distpath $pyInstallerDist --workpath $pyInstallerWork "
            "--specpath $pyInstallerSpec --name Autosport $trustedGuiEntry"
        )
        first_bind = (
            "python $sourceVerifier --bind-artifact $builtAutosportExe "
            "--bound-output $boundAutosportExe --digest-output $autosportDigestPath"
        )
        second_build = (
            "& $packagingPython -I -m PyInstaller --noconfirm --clean --onefile --console "
            "--paths $trustedBuildSrc --distpath $pyInstallerDist --workpath $pyInstallerWork "
            "--specpath $pyInstallerSpec --name Autosport-Data $trustedDataEntry"
        )
        second_bind = (
            "python $sourceVerifier --bind-artifact $builtDataExe "
            "--bound-output $boundDataExe --digest-output $dataDigestPath"
        )
        verify_gui = "python $sourceVerifier --verify-artifact $boundAutosportExe --expected-sha256 $autosportExeSha256"
        verify_data = "python $sourceVerifier --verify-artifact $boundDataExe --expected-sha256 $dataExeSha256"
        package_build = "python scripts/package_windows.py `"

        self.assertLess(script.index(first_build), script.index(first_bind))
        self.assertLess(script.index(second_build), script.index(second_bind))
        self.assertIn("Start-Process -FilePath $boundAutosportExe", script)
        self.assertIn("$dataExe = $boundDataExe", script)
        self.assertLess(script.index(verify_gui), script.index(package_build))
        self.assertLess(script.index(verify_data), script.index(package_build))
        self.assertIn("--exe $boundAutosportExe `", script)
        self.assertIn("--data-exe $boundDataExe `", script)

    def test_windows_workflow_checks_out_and_preflights_exact_candidate_before_build(self) -> None:
        workflow = Path(".github/workflows/windows-build.yml").read_text(encoding="utf-8")
        exact_ref = "ref: ${{ github.event.pull_request.head.sha || github.sha }}"
        preflight = "python scripts/verify_source_checkout.py --source-sha $env:AUTOSPORT_SOURCE_SHA"
        build = "run: ./scripts/build_windows_candidate.ps1"
        self.assertIn(exact_ref, workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn(preflight, workflow)
        self.assertLess(workflow.index(preflight), workflow.index(build))

    def test_post_build_release_consumers_use_verified_package_extractions(self) -> None:
        workflow = Path(".github/workflows/windows-build.yml").read_text(encoding="utf-8")
        nvda_smoke = Path("scripts/nvda_evidence_package_smoke.ps1").read_text(encoding="utf-8")
        walk_forward = Path("scripts/walk_forward_package_smoke.ps1").read_text(encoding="utf-8")
        forecast_origin = Path("scripts/walk_forward_origin_package_smoke.ps1").read_text(encoding="utf-8")

        self.assertIn("name: Materialize verified independent package extraction", workflow)
        self.assertIn("AUTOSPORT_PACKAGED_EXE=", workflow)
        self.assertIn("AUTOSPORT_PACKAGED_DATA_EXE=", workflow)
        self.assertIn("Independent package Autosport.exe hash mismatch", workflow)
        self.assertIn("Independent package Autosport-Data.exe hash mismatch", workflow)
        self.assertNotIn("Join-Path $PWD 'dist/Autosport-Data.exe'", workflow)
        self.assertNotIn("Join-Path $PWD 'dist/Autosport.exe'", workflow)

        for script in (nvda_smoke, walk_forward, forecast_origin):
            self.assertIn("$env:AUTOSPORT_PACKAGED_DATA_EXE", script)
            self.assertNotIn("Join-Path $PWD 'dist/Autosport-Data.exe'", script)


if __name__ == "__main__":
    unittest.main()
