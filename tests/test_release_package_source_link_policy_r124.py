"""Plan 6/6Б: packaging must not follow untrusted source links or publish private state.

All content is synthetic. A failed build must preserve an existing published ZIP.
"""
from __future__ import annotations

import os
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


def _link(source: Path, destination: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(source, destination, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable on this host: {type(exc).__name__}")


def _assert_no_publication(f: dict[str, Path], tmp_path: Path) -> None:
    package = tmp_path / "published.zip"
    package.write_bytes(b"existing-good-package")
    with pytest.raises(ValueError, match="unsafe release source|forbidden private/test state"):
        _build(f, package)
    assert package.read_bytes() == b"existing-good-package"
    assert not list(tmp_path.glob(".autosport-release-stage-*"))
    assert not list(tmp_path.glob(".published.zip.*.tmp"))


def test_examples_file_symlink_cannot_copy_external_private_bytes(tmp_path):
    f = _fixture(tmp_path)
    private = tmp_path / "outside-private.txt"
    private.write_bytes(b"synthetic-private-data-not-for-package")
    _link(private, f["examples"] / "public-demo.txt")
    _assert_no_publication(f, tmp_path)


def test_examples_directory_symlink_cannot_traverse_external_tree(tmp_path):
    f = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "synthetic.txt").write_text("not-for-package", encoding="utf-8")
    _link(outside, f["examples"] / "shared", directory=True)
    _assert_no_publication(f, tmp_path)


def test_fixed_executable_symlink_cannot_be_packaged(tmp_path):
    f = _fixture(tmp_path)
    target = tmp_path / "outside.exe"
    target.write_bytes(b"synthetic-not-an-executable")
    f["exe"].unlink()
    _link(target, f["exe"])
    _assert_no_publication(f, tmp_path)


def test_example_root_symlink_cannot_be_packaged(tmp_path):
    f = _fixture(tmp_path)
    outside = tmp_path / "outside-examples"
    outside.mkdir()
    (outside / "demo.txt").write_text("synthetic", encoding="utf-8")
    for entry in f["examples"].iterdir():
        entry.unlink()
    f["examples"].rmdir()
    _link(outside, f["examples"], directory=True)
    _assert_no_publication(f, tmp_path)


@pytest.mark.parametrize("relative", [".env", "nested/token.json", "nested/.pytest_cache/state"])
def test_writer_rejects_forbidden_example_member_before_publication(tmp_path, relative):
    f = _fixture(tmp_path)
    path = f["examples"] / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic-not-for-package", encoding="utf-8")
    _assert_no_publication(f, tmp_path)


def test_benign_example_files_remain_reproducible(tmp_path):
    f = _fixture(tmp_path)
    (f["examples"] / "Українська назва.txt").write_text("приклад", encoding="utf-8")
    package = tmp_path / "published.zip"
    _build(f, package)
    first = package.read_bytes()
    assert release.verify_windows_package(package, expected_source_sha=SOURCE_SHA)["status"] == "PASS"
    _build(f, package)
    assert package.read_bytes() == first


def test_source_swapped_to_link_after_preflight_is_rejected(tmp_path, monkeypatch):
    f = _fixture(tmp_path)
    outside = tmp_path / "synthetic-outside.exe"
    outside.write_bytes(b"synthetic-external-data")
    original = release._require_release_regular_source
    swapped = False

    def swap_after_precheck(path):
        nonlocal swapped
        original(path)
        if path == f["exe"] and not swapped:
            swapped = True
            path.unlink()
            _link(outside, path)

    monkeypatch.setattr(release, "_require_release_regular_source", swap_after_precheck)
    _assert_no_publication(f, tmp_path)
    assert swapped


def test_link_inserted_after_example_scan_is_rejected(tmp_path, monkeypatch):
    f = _fixture(tmp_path)
    outside = tmp_path / "synthetic-outside.txt"
    outside.write_bytes(b"synthetic-external-data")
    original = release._copy_release_example_tree
    inserted = False

    def copytree_after_insertion(source, destination, *args, **kwargs):
        nonlocal inserted
        if not inserted:
            inserted = True
            _link(outside, f["examples"] / "late-link.txt")
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(release, "_copy_release_example_tree", copytree_after_insertion)
    _assert_no_publication(f, tmp_path)
    assert inserted
