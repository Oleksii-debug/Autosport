"""Plan 6/6Б: an example source cannot change while the release is staged.

Synthetic fixtures only. A failed attempt must preserve the prior published ZIP.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from autosport import release_package as release
from test_windows_update_release_package_integration import SOURCE_SHA, _fixture


def _build(f: dict[str, Path], destination: Path) -> None:
    release.build_windows_package(
        f["exe"], f["start"], f["examples"], f["diagnostic"],
        f["accessibility"], f["keyboard"], f["restart"], destination,
        SOURCE_SHA, update_version="1.2.3", update_schema=2,
    )


def _unchanged_on_rejection(f: dict[str, Path], tmp_path: Path) -> None:
    package = tmp_path / "previous.zip"
    package.write_bytes(b"old-published-bytes")
    with pytest.raises(ValueError, match="unsafe release source"):
        _build(f, package)
    assert package.read_bytes() == b"old-published-bytes"
    assert not list(tmp_path.glob(".autosport-release-stage-*"))


def test_example_replaced_by_symlink_during_file_copy_fails_closed(tmp_path, monkeypatch):
    f = _fixture(tmp_path)
    original = release._copy_release_regular_source
    outside = tmp_path / "synthetic-outside.txt"
    outside.write_bytes(b"not-for-release")
    changed = False

    def swap_before_copy(src, dst, *args, **kwargs):
        nonlocal changed
        if not changed and Path(os.fspath(src)).name == "demo.jsonl":
            changed = True
            source = f["examples"] / "demo.jsonl"
            source.unlink()
            try:
                os.symlink(outside, source)
            except (OSError, NotImplementedError) as exc:
                pytest.skip(f"symlink unavailable: {type(exc).__name__}")
        return original(src, dst, *args, **kwargs)

    monkeypatch.setattr(release, "_copy_release_regular_source", swap_before_copy)
    _unchanged_on_rejection(f, tmp_path)
    assert changed


def test_example_regular_file_replaced_during_copy_fails_closed(tmp_path, monkeypatch):
    f = _fixture(tmp_path)
    original = release._copy_release_regular_source
    changed = False

    def swap_regular(src, dst, *args, **kwargs):
        nonlocal changed
        if not changed and Path(os.fspath(src)).name == "demo.jsonl":
            changed = True
            source = f["examples"] / "demo.jsonl"
            replacement = tmp_path / "replacement.txt"
            replacement.write_bytes(b"different-file-with-same-path")
            os.replace(replacement, source)
        return original(src, dst, *args, **kwargs)

    monkeypatch.setattr(release, "_copy_release_regular_source", swap_regular)
    _unchanged_on_rejection(f, tmp_path)
    assert changed


def test_fixed_executable_in_place_mutation_during_copy_fails_closed(tmp_path, monkeypatch):
    f = _fixture(tmp_path)
    original = shutil.copyfileobj
    changed = False

    def mutate_during_copy(source, destination, *args, **kwargs):
        nonlocal changed
        result = original(source, destination, *args, **kwargs)
        if (not changed and os.fstat(source.fileno()).st_ino == f["exe"].stat().st_ino):
            changed = True
            with f["exe"].open("ab") as writer:
                writer.write(b"changed-after-read")
        return result

    monkeypatch.setattr(shutil, "copyfileobj", mutate_during_copy)
    _unchanged_on_rejection(f, tmp_path)
    assert changed


def test_stable_nested_unicode_examples_are_reproducible(tmp_path):
    f = _fixture(tmp_path)
    nested = f["examples"] / "Каталог з пробілами"
    nested.mkdir()
    (nested / "приклад.txt").write_text("Зразок\n", encoding="utf-8")
    package = tmp_path / "published.zip"
    _build(f, package)
    original = package.read_bytes()
    assert release.verify_windows_package(package, expected_source_sha=SOURCE_SHA)["status"] == "PASS"
    _build(f, package)
    assert package.read_bytes() == original
