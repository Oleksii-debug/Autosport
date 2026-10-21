from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from autosport.release_package import build_windows_package


class ReleaseSourcePreflightTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40

    def _inputs(self, root: Path) -> tuple[Path, ...]:
        root.mkdir(parents=True, exist_ok=True)
        paths = [
            root / "Autosport.exe",
            root / "WINDOWS_START_HERE.txt",
            root / "diagnostic.json",
            root / "accessibility.json",
            root / "keyboard.json",
            root / "restart.json",
        ]
        for path in paths:
            path.write_bytes(b"fixture")
        examples = root / "tt_demo"
        examples.mkdir()
        (examples / "market.jsonl").write_text("{}\n", encoding="utf-8")
        return (
            paths[0], paths[1], examples, *paths[2:],
            root / "Autosport-V1-windows-x64.zip", self.SOURCE_SHA,
        )

    @staticmethod
    def _build(args: tuple[Path, ...]) -> None:
        build_windows_package(*args)

    def test_regular_example_tree_remains_buildable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self._inputs(Path(temporary))
            self._build(args)
            with zipfile.ZipFile(args[-2]) as archive:
                self.assertIn("Autosport-V1/examples/tt_demo/market.jsonl", archive.namelist())

    def test_private_example_file_is_rejected_before_output_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self._inputs(Path(temporary))
            (args[2] / ".env").write_text("SECRET_TEST_SENTINEL", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "forbidden private/test state"):
                self._build(args)
            self.assertFalse(args[-2].exists())

    def test_test_cache_directory_is_rejected_before_output_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self._inputs(Path(temporary))
            cache = args[2] / "__pycache__"
            cache.mkdir()
            (cache / "data.pyc").write_bytes(b"test")
            with self.assertRaisesRegex(ValueError, "forbidden private/test state"):
                self._build(args)
            self.assertFalse(args[-2].exists())

    def test_nonregular_example_source_is_rejected_before_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self._inputs(Path(temporary))
            outside = Path(temporary) / "outside.txt"
            outside.write_text("SECRET_TEST_SENTINEL", encoding="utf-8")
            link = args[2] / "market-link.jsonl"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation unavailable on this runner")
            with self.assertRaisesRegex(ValueError, "symlink or nonregular"):
                self._build(args)
            self.assertFalse(args[-2].exists())

    def test_symlinked_executable_or_example_root_is_not_followed(self) -> None:
        for replace_example_root in (False, True):
            with self.subTest(example_root=replace_example_root):
                with tempfile.TemporaryDirectory() as temporary:
                    args = list(self._inputs(Path(temporary)))
                    real_path = args[2] if replace_example_root else args[0]
                    substitute = Path(temporary) / "link"
                    try:
                        substitute.symlink_to(
                            real_path, target_is_directory=replace_example_root,
                        )
                    except (OSError, NotImplementedError):
                        self.skipTest("symlink creation unavailable on this runner")
                    args[2 if replace_example_root else 0] = substitute
                    with self.assertRaisesRegex(ValueError, "non-symlink"):
                        self._build(tuple(args))
                    self.assertFalse(args[-2].exists())

    @unittest.skipIf(os.name == "nt", "case-sensitive duplicate inputs need POSIX FS")
    def test_casefold_colliding_example_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self._inputs(Path(temporary))
            (args[2] / "MARKET.JSONL").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Windows path collision"):
                self._build(args)
            self.assertFalse(args[-2].exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO creation unavailable")
    def test_fifo_input_cannot_block_package_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = self._inputs(Path(temporary))
            fifo = args[2] / "blocked.jsonl"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, "symlink or nonregular"):
                self._build(args)
            self.assertFalse(args[-2].exists())


if __name__ == "__main__":
    unittest.main()
