"""Windows candidate packaging must not destroy a nearby installed Autosport tree."""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from autosport import release_package


class ReleasePackageInstalledTreePreservationTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40

    @staticmethod
    def _inputs(root: Path) -> tuple[Path, ...]:
        root.mkdir(parents=True)
        exe = root / "Autosport.exe"
        guide = root / "WINDOWS_START_HERE.txt"
        example = root / "example"
        exe.write_bytes(b"fixture-executable")
        guide.write_text("Synthetic start guide\n", encoding="utf-8")
        example.mkdir()
        (example / "market.jsonl").write_bytes(b'{"fixture":true}\n')
        reports = []
        for name in ("diagnostic", "accessibility", "keyboard", "restart"):
            path = root / (name + ".json")
            path.write_text("{}\n", encoding="utf-8")
            reports.append(path)
        return (exe, guide, example, *reports)

    def _build(self, inputs: tuple[Path, ...], archive: Path) -> tuple[Path, str]:
        return release_package.build_windows_package(
            *inputs, archive, self.SOURCE_SHA
        )

    @staticmethod
    def _installed(destination: Path) -> tuple[Path, Path]:
        installed = destination / "Autosport-V1"
        private = installed / "profile with spaces" / "дані користувача"
        private.mkdir(parents=True)
        sentinel = private / "synthetic-state.txt"
        sentinel.write_bytes(b"original-data-not-real-secrets")
        (installed / "Autosport.exe").write_bytes(b"installed-executable")
        return installed, sentinel

    @staticmethod
    def _assert_no_staging(destination: Path) -> None:
        assert list(destination.glob(".autosport-release-stage-*")) == []

    def test_zip_is_deterministic_without_mutating_existing_install(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            inputs = self._inputs(root / "inputs")
            destination = root / "output with spaces Ω"
            destination.mkdir()
            installed, sentinel = self._installed(destination)
            archive = destination / "candidate.zip"
            first_path, first_digest = self._build(inputs, archive)
            self.assertEqual(first_path, archive)
            original_archive = archive.read_bytes()
            with zipfile.ZipFile(archive) as zf:
                self.assertEqual(
                    zf.read("Autosport-V1/Autosport.exe"),
                    b"fixture-executable",
                )
                self.assertFalse(
                    any("synthetic-state" in name for name in zf.namelist())
                )
            self.assertEqual(
                sentinel.read_bytes(), b"original-data-not-real-secrets"
            )
            self.assertEqual(
                (installed / "Autosport.exe").read_bytes(),
                b"installed-executable",
            )
            self._assert_no_staging(destination)
            _, second_digest = self._build(inputs, archive)
            self.assertEqual(first_digest, second_digest)
            self.assertEqual(archive.read_bytes(), original_archive)
            self._assert_no_staging(destination)

    def test_failed_copy_preserves_previous_zip_and_installed_state(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            inputs = self._inputs(root / "inputs")
            destination = root / "published"
            destination.mkdir()
            installed, sentinel = self._installed(destination)
            archive = destination / "candidate.zip"
            self._build(inputs, archive)
            previous_zip = archive.read_bytes()
            with patch.object(
                release_package.shutil,
                "copy2",
                side_effect=OSError("synthetic interrupted package copy"),
            ):
                with self.assertRaisesRegex(OSError, "synthetic interrupted"):
                    self._build(inputs, archive)
            self.assertEqual(archive.read_bytes(), previous_zip)
            self.assertEqual(
                sentinel.read_bytes(), b"original-data-not-real-secrets"
            )
            self.assertEqual(
                (installed / "Autosport.exe").read_bytes(),
                b"installed-executable",
            )
            self._assert_no_staging(destination)
