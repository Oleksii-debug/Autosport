"""Plan-6 exact-source qualifier must not execute tracked symlinked fixed tests.

These tests simulate a Git-clean tracked link without requiring Windows
Developer Mode or elevated symlink privileges. The real Git+pytest
counterexample is recorded in PR evidence; this suite tests the guard.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/qualify_plan6_source.py"
spec = importlib.util.spec_from_file_location("plan6_a5_fixed_symlink_guard", SCRIPT)
assert spec is not None and spec.loader is not None
qualifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualifier)


def _test_path(root: Path, name: str) -> Path:
    path = root / "tests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def test_safe(): assert True\n", encoding="utf-8")
    return path


def test_fixed_group_symlink_rejected_without_pytest_execution() -> None:
    """A tracked fixed test link is untrusted even if Path.is_file is true."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        linked = _test_path(root, "test_localization_catalog.py")
        original_is_symlink = Path.is_symlink
        with patch.object(
            Path,
            "is_symlink",
            lambda path: path == linked or original_is_symlink(path),
        ):
            with patch.object(qualifier.subprocess, "run") as execute:
                result = qualifier._test_group(
                    root, "uk_localization",
                    ("tests/test_localization_catalog.py",), timeout=3,
                )
                execute.assert_not_called()
        assert result["status"] == "FAIL_MISSING_TEST_SOURCE"
        assert result["passed"] == 0


def test_one_link_invalidates_entire_fixed_group() -> None:
    """No partial PASS if only one of several fixed test paths is linked."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _test_path(root, "test_localization_catalog.py")
        linked = _test_path(root, "test_gui_localization_contract.py")
        original_is_symlink = Path.is_symlink
        with patch.object(
            Path,
            "is_symlink",
            lambda path: path == linked or original_is_symlink(path),
        ):
            with patch.object(qualifier.subprocess, "run") as execute:
                result = qualifier._test_group(
                    root, "uk_localization",
                    ("tests/test_localization_catalog.py",
                     "tests/test_gui_localization_contract.py"), timeout=3,
                )
                execute.assert_not_called()
        assert result["status"] == "FAIL_MISSING_TEST_SOURCE"


def test_regular_fixed_source_still_executes_and_requires_pass() -> None:
    """The guard must not disable normal exact-head test execution."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        _test_path(root, "test_localization_catalog.py")
        with patch.object(qualifier.subprocess, "run") as execute:
            execute.return_value.returncode = 0
            execute.return_value.stdout = "1 passed in 0.05s"
            execute.return_value.stderr = ""
            result = qualifier._test_group(
                root, "uk_localization",
                ("tests/test_localization_catalog.py",), timeout=3,
            )
            execute.assert_called_once()
        assert result["status"] == "PASS"
        assert result["passed"] == 1


def _real_git_fixture(root: Path) -> Path:
    """Make a disposable real Git HEAD with one tracked Python test file."""
    root.mkdir(parents=True)
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True,
            text=True, timeout=20,
        ).stdout.strip()
    git("init", "-q")
    git("config", "core.autocrlf", "false")
    git("config", "user.name", "A5 regression fixture")
    git("config", "user.email", "a5@example.invalid")
    source = _test_path(root, "test_localization_catalog.py")
    git("add", "tests/test_localization_catalog.py")
    git("commit", "-qm", "fixture")
    return source


def test_real_git_clean_head_remains_qualifiable() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "проєкт з пробілом"
        _real_git_fixture(root)
        assert qualifier._identity(root) == subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()


@pytest.mark.parametrize("index_flag", ["assume-unchanged", "skip-worktree"])
def test_real_git_masked_tracked_source_rejected(index_flag: str) -> None:
    """Git status says CLEAN while a tracked test differs from HEAD."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "проєкт з пробілом"
        source = _real_git_fixture(root)
        subprocess.run(
            ["git", "update-index", f"--{index_flag}",
             "tests/test_localization_catalog.py"],
            cwd=root, check=True, capture_output=True, text=True, timeout=20,
        )
        source.write_text("def test_safe(): assert False\\n", encoding="utf-8")
        assert not subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root, check=True, capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        with pytest.raises(ValueError, match="source index masks tracked changes"):
            qualifier._identity(root)
