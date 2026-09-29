from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.release_package as release_package


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
        for key in ("exe", "start", "diagnostic", "accessibility", "keyboard", "restart"):
            paths[key].write_bytes(f"{key}-payload".encode("ascii"))
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


if __name__ == "__main__":
    unittest.main()
