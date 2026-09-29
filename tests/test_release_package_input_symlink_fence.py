from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import autosport.release_package as release_package
from autosport.data_tool_package import bind_portable_data_tool, verify_portable_data_tool


class ReleasePackageInputSymlinkFenceTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40

    def _make_inputs(self, root: Path) -> dict[str, Path]:
        root.mkdir(parents=True, exist_ok=True)
        example = root / "example"
        example.mkdir()
        (example / "market.jsonl").write_text('{"market":"demo"}\n', encoding="utf-8")

        paths = {
            "exe": root / "Autosport.exe",
            "start": root / "WINDOWS_START_HERE.txt",
            "example": example,
            "diagnostic": root / "diagnostic.json",
            "accessibility": root / "accessibility.json",
            "keyboard": root / "keyboard.json",
            "restart": root / "restart.json",
            "package": root / "candidate.zip",
        }
        paths["exe"].write_bytes(b"autosport-executable")
        paths["start"].write_text("start\n", encoding="utf-8")

        common = {
            "status": "PASS",
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        for key in ("diagnostic", "accessibility", "keyboard"):
            paths[key].write_text(
                json.dumps(common) + "\n",
                encoding="utf-8",
            )
        paths["restart"].write_text(
            json.dumps(
                {
                    **common,
                    "session_restart_status": "PASS",
                    "transaction_recovery_status": "PASS",
                    "recovery_disposition": "aborted_uncommitted",
                    "process_kill_relaunch_status": "PASS",
                    "process_kill_stage_pid": 101,
                    "process_kill_return_code": -15,
                    "process_recovery_pid": 202,
                    "process_recovery_run_id": "symlink-fence-regression",
                    "process_recovery_disposition": "committed",
                    "process_recovery_registry_status": "completed",
                    "process_recovery_manifest_phase": "completed",
                    "process_recovery_base_paper_book_sha256": "1" * 64,
                    "process_recovery_base_decision_ledger_sha256": "2" * 64,
                    "process_recovery_new_paper_book_sha256": "3" * 64,
                    "process_recovery_new_decision_ledger_sha256": "4" * 64,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return paths

    def _symlink_or_skip(self, target: Path, link: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlinks are unavailable in this environment: {exc}")

    def _build(self, paths: dict[str, Path]) -> None:
        release_package.build_windows_package(
            paths["exe"],
            paths["start"],
            paths["example"],
            paths["diagnostic"],
            paths["accessibility"],
            paths["keyboard"],
            paths["restart"],
            paths["package"],
            self.SOURCE_SHA,
        )

    def test_top_level_symlink_fails_before_existing_staging_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            secret = root / "outside-secret.bin"
            secret.write_bytes(b"must-not-enter-package")
            paths["exe"].unlink()
            self._symlink_or_skip(secret, paths["exe"])

            staging = root / "Autosport-V1"
            staging.mkdir()
            marker = staging / "keep.txt"
            marker.write_text("preserve-on-preflight-failure", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "Autosport executable input must not be a symbolic link",
            ):
                self._build(paths)

            self.assertEqual(
                marker.read_text(encoding="utf-8"),
                "preserve-on-preflight-failure",
            )
            self.assertFalse(paths["package"].exists())

    def test_nested_example_symlink_is_rejected_before_packaging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            secret = root / "outside-secret.json"
            secret.write_text('{"secret":true}\n', encoding="utf-8")
            self._symlink_or_skip(secret, paths["example"] / "external.json")

            staging = root / "Autosport-V1"
            staging.mkdir()
            marker = staging / "keep.txt"
            marker.write_text("preserve-on-preflight-failure", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "release example tree contains a symbolic link: external.json",
            ):
                self._build(paths)

            self.assertTrue(marker.exists())
            self.assertFalse(paths["package"].exists())


    def test_top_level_windows_reparse_file_fails_before_existing_staging_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            executable = paths["exe"]
            real_lstat = Path.lstat

            staging = root / "Autosport-V1"
            staging.mkdir()
            marker = staging / "keep.txt"
            marker.write_text("preserve-on-preflight-failure", encoding="utf-8")

            def lstat_with_reparse(path: Path):
                metadata = real_lstat(path)
                if path == executable:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_file_attributes=release_package._WINDOWS_REPARSE_POINT_FLAG,
                    )
                return metadata

            with patch.object(Path, "lstat", new=lstat_with_reparse):
                with self.assertRaisesRegex(
                    ValueError,
                    "Autosport executable input must not be a Windows reparse point",
                ):
                    self._build(paths)

            self.assertEqual(
                marker.read_text(encoding="utf-8"),
                "preserve-on-preflight-failure",
            )
            self.assertFalse(paths["package"].exists())

    def test_nested_example_windows_reparse_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            example_entry = paths["example"] / "market.jsonl"
            real_lstat = Path.lstat

            def lstat_with_reparse(path: Path):
                metadata = real_lstat(path)
                if path == example_entry:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_file_attributes=release_package._WINDOWS_REPARSE_POINT_FLAG,
                    )
                return metadata

            with patch.object(Path, "lstat", new=lstat_with_reparse):
                with self.assertRaisesRegex(
                    ValueError,
                    "release example tree contains a Windows reparse point: market.jsonl",
                ):
                    self._build(paths)

            self.assertFalse(paths["package"].exists())

    def test_example_tree_depth_is_bounded_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            current = paths["example"]
            for _ in range(release_package._MAX_RELEASE_SOURCE_TREE_DEPTH + 1):
                current = current / "d"
                current.mkdir()

            staging = root / "Autosport-V1"
            staging.mkdir()
            marker = staging / "keep.txt"
            marker.write_text("preserve-on-preflight-failure", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "release example tree exceeds supported directory depth",
            ):
                self._build(paths)

            self.assertEqual(
                marker.read_text(encoding="utf-8"),
                "preserve-on-preflight-failure",
            )
            self.assertFalse(paths["package"].exists())

    def test_real_windows_nested_junction_is_rejected(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows junction semantics require an NT runner")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            outside = root / "outside-tree"
            outside.mkdir()
            (outside / "outside-secret.json").write_text(
                '{"secret":true}\n',
                encoding="utf-8",
            )
            junction = paths["example"] / "external-junction"
            created = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(junction),
                    str(outside),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest(
                    "Windows runner cannot create a directory junction: "
                    + (created.stderr or created.stdout).strip()
                )

            with self.assertRaisesRegex(
                ValueError,
                "release example tree contains a Windows reparse point: external-junction",
            ):
                self._build(paths)

            self.assertFalse(paths["package"].exists())

    def test_reparse_directory_added_after_preflight_is_rejected_before_file_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            late = paths["example"] / "late-reparse"
            secret = late / "outside-secret.json"
            real_require = release_package._require_regular_source_tree
            real_lstat = Path.lstat
            real_read = release_package._read_regular_source_bytes
            injected = False
            secret_read = False

            def validate_then_inject(path: Path, *, label: str) -> None:
                nonlocal injected
                real_require(path, label=label)
                if path == paths["example"] and not injected:
                    injected = True
                    late.mkdir()
                    secret.write_text('{"secret":true}\n', encoding="utf-8")

            def lstat_with_reparse(path: Path):
                metadata = real_lstat(path)
                if path == late:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_file_attributes=release_package._WINDOWS_REPARSE_POINT_FLAG,
                    )
                return metadata

            def read_without_secret(path: Path, *, label: str) -> bytes:
                nonlocal secret_read
                if path == secret:
                    secret_read = True
                    raise AssertionError("late reparse target file must never be read")
                return real_read(path, label=label)

            with (
                patch.object(
                    release_package,
                    "_require_regular_source_tree",
                    side_effect=validate_then_inject,
                ),
                patch.object(Path, "lstat", new=lstat_with_reparse),
                patch.object(
                    release_package,
                    "_read_regular_source_bytes",
                    side_effect=read_without_secret,
                ),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "release example tree contains a Windows reparse point: late-reparse",
                ):
                    self._build(paths)

            self.assertTrue(injected)
            self.assertFalse(secret_read)
            self.assertFalse(paths["package"].exists())

    def test_real_windows_junction_added_after_preflight_is_not_traversed(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows junction semantics require an NT runner")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            outside = root / "outside-tree"
            outside.mkdir()
            outside_secret = outside / "outside-secret.json"
            outside_secret.write_text('{"secret":true}\n', encoding="utf-8")
            junction = paths["example"] / "late-junction"
            real_require = release_package._require_regular_source_tree
            injected = False

            def validate_then_inject(path: Path, *, label: str) -> None:
                nonlocal injected
                real_require(path, label=label)
                if path != paths["example"] or injected:
                    return
                created = subprocess.run(
                    [
                        "cmd.exe",
                        "/d",
                        "/c",
                        "mklink",
                        "/J",
                        str(junction),
                        str(outside),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if created.returncode != 0:
                    self.skipTest(
                        "Windows runner cannot create a directory junction: "
                        + (created.stderr or created.stdout).strip()
                    )
                injected = True

            with patch.object(
                release_package,
                "_require_regular_source_tree",
                side_effect=validate_then_inject,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "release example tree contains a Windows reparse point: late-junction",
                ):
                    self._build(paths)

            self.assertTrue(injected)
            staged_secret = (
                root
                / "Autosport-V1"
                / "examples"
                / paths["example"].name
                / "late-junction"
                / outside_secret.name
            )
            self.assertFalse(staged_secret.exists())
            self.assertFalse(paths["package"].exists())

    def test_regular_to_symlink_swap_during_copy_is_preserved_then_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            secret = root / "outside-secret.bin"
            secret.write_bytes(b"must-not-be-followed")
            executable = paths["exe"]

            real_copy = release_package._copy2_no_follow
            swapped = False

            def swap_then_copy(source: str | Path, destination: str | Path) -> str:
                nonlocal swapped
                source_path = Path(source)
                if source_path == executable and not swapped:
                    swapped = True
                    executable.unlink()
                    self._symlink_or_skip(secret, executable)
                return real_copy(source, destination)

            with patch.object(
                release_package,
                "_copy2_no_follow",
                side_effect=swap_then_copy,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "release package staging tree contains a symbolic link: Autosport.exe",
                ):
                    self._build(paths)

            self.assertTrue(swapped)
            self.assertFalse(paths["package"].exists())

    def test_release_verifier_package_symlink_is_rejected_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            self._build(paths)

            package_link = root / "verify-link.zip"
            self._symlink_or_skip(paths["package"], package_link)

            with self.assertRaisesRegex(
                ValueError,
                "release package input must not be a symbolic link",
            ):
                release_package.verify_windows_package(
                    package_link,
                    expected_source_sha=self.SOURCE_SHA,
                )

    def test_release_verifier_regular_to_symlink_race_never_uses_path_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            self._build(paths)

            package_path = paths["package"]
            outside = root / "outside-package.zip"
            outside.write_bytes(package_path.read_bytes())
            real_require = release_package._require_regular_source_file
            real_path_open = Path.open
            swapped = False
            follow_open_attempted = False

            def validate_then_swap(path: Path, *, label: str) -> None:
                nonlocal swapped
                real_require(path, label=label)
                if Path(path) == package_path and not swapped:
                    swapped = True
                    package_path.unlink()
                    self._symlink_or_skip(outside, package_path)

            def forbid_following_path_open(path: Path, *args, **kwargs):
                nonlocal follow_open_attempted
                if Path(path) == package_path:
                    follow_open_attempted = True
                    raise AssertionError(
                        "release verifier input must use the canonical no-follow descriptor"
                    )
                return real_path_open(path, *args, **kwargs)

            with (
                patch.object(
                    release_package,
                    "_require_regular_source_file",
                    side_effect=validate_then_swap,
                ),
                patch.object(Path, "open", new=forbid_following_path_open),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "release package input changed during open",
                ):
                    release_package.verify_windows_package(
                        package_path,
                        expected_source_sha=self.SOURCE_SHA,
                    )

            self.assertTrue(swapped)
            self.assertFalse(follow_open_attempted)

    def test_portable_data_executable_symlink_is_rejected_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            self._build(paths)

            secret = root / "outside-data-tool.exe"
            secret.write_bytes(b"external-data-tool")
            data_link = root / "Autosport-Data.exe"
            self._symlink_or_skip(secret, data_link)

            with self.assertRaisesRegex(
                ValueError,
                "portable data tool executable input must not be a symbolic link",
            ):
                bind_portable_data_tool(paths["package"], data_link)

    def test_portable_data_base_package_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            self._build(paths)

            package_link = root / "candidate-link.zip"
            self._symlink_or_skip(paths["package"], package_link)

            with self.assertRaisesRegex(
                ValueError,
                "base release package input must not be a symbolic link",
            ):
                verify_portable_data_tool(package_link)


    def test_portable_data_executable_regular_to_symlink_race_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            self._build(paths)

            data_path = root / "Autosport-Data.exe"
            data_path.write_bytes(b"original-data-tool")
            outside = root / "outside-data-tool.exe"
            outside.write_bytes(b"must-not-be-read")
            real_require = release_package._require_regular_source_file
            real_path_open = Path.open
            swapped = False
            follow_open_attempted = False

            def validate_then_swap(path: Path, *, label: str) -> None:
                nonlocal swapped
                real_require(path, label=label)
                if Path(path) == data_path and not swapped:
                    swapped = True
                    data_path.unlink()
                    self._symlink_or_skip(outside, data_path)

            def forbid_following_path_open(path: Path, *args, **kwargs):
                nonlocal follow_open_attempted
                if Path(path) == data_path:
                    follow_open_attempted = True
                    raise AssertionError(
                        "portable data tool input must use the canonical no-follow descriptor"
                    )
                return real_path_open(path, *args, **kwargs)

            with (
                patch.object(
                    release_package,
                    "_require_regular_source_file",
                    side_effect=validate_then_swap,
                ),
                patch.object(Path, "open", new=forbid_following_path_open),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "portable data tool executable input changed during open",
                ):
                    bind_portable_data_tool(paths["package"], data_path)

            self.assertTrue(swapped)
            self.assertFalse(follow_open_attempted)

    def test_portable_base_package_regular_to_symlink_race_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._make_inputs(root)
            self._build(paths)

            outside = root / "outside-package.zip"
            outside.write_bytes(paths["package"].read_bytes())
            package_path = paths["package"]
            real_require = release_package._require_regular_source_file
            swapped = False

            def validate_then_swap(path: Path, *, label: str) -> None:
                nonlocal swapped
                real_require(path, label=label)
                if Path(path) == package_path and not swapped:
                    swapped = True
                    package_path.unlink()
                    self._symlink_or_skip(outside, package_path)

            with patch.object(
                release_package,
                "_require_regular_source_file",
                side_effect=validate_then_swap,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "base release package input changed during open",
                ):
                    verify_portable_data_tool(package_path)

            self.assertTrue(swapped)


if __name__ == "__main__":
    unittest.main()
