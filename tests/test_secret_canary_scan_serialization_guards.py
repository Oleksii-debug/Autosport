from __future__ import annotations

import json
from pathlib import Path

import pytest

import autosport.secret_canary_scan as secret_canary_scan


@pytest.mark.parametrize("ensure_ascii", [False, True])
def test_json_string_serialization_cannot_hide_canary(
    tmp_path: Path,
    ensure_ascii: bool,
) -> None:
    canary = 'oauth-"\\-АВТ-\n-secret'
    serialized = json.dumps(canary, ensure_ascii=ensure_ascii)[1:-1]
    encoding = "ascii" if ensure_ascii else "utf-8"
    payload = serialized.encode(encoding)
    assert canary.encode("utf-8") not in payload

    (tmp_path / "session.json").write_bytes(b'"token":"' + payload + b'"')

    report = secret_canary_scan.scan_secret_canary(
        tmp_path,
        canary,
        chunk_size=3,
    )

    assert report.status == "LEAK"
    assert report.exit_code == 2
    assert len(report.findings) == 1
    expected = "json-string-ascii" if ensure_ascii else "json-string-utf8"
    assert expected in report.findings[0].encodings
    assert canary not in repr(report)


def test_windows_reparse_attribute_is_treated_as_link_like() -> None:
    class FakeStat:
        st_file_attributes = secret_canary_scan._REPARSE_POINT_FLAG

    class FakePath:
        def lstat(self) -> FakeStat:
            return FakeStat()

    assert secret_canary_scan._path_is_reparse_point(FakePath()) is True  # type: ignore[arg-type]


def test_reparse_subtree_fails_closed_without_descent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    canary = "planted-secret"
    junction = tmp_path / "junction"
    junction.mkdir()
    (junction / "hidden.txt").write_text(canary, encoding="utf-8")
    original = secret_canary_scan._path_is_reparse_point

    def fake_reparse(path: Path) -> bool:
        if path == junction:
            return True
        return original(path)

    monkeypatch.setattr(secret_canary_scan, "_path_is_reparse_point", fake_reparse)

    report = secret_canary_scan.scan_secret_canary(tmp_path, canary)

    assert report.status == "INCOMPLETE"
    assert report.exit_code == 3
    assert report.scanned_files == 0
    assert report.findings == ()
    assert any(
        error.error_type == "ReparsePointRejected"
        for error in report.errors
    )


def test_reparse_fixture_exclusion_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    canary = "planted-secret"
    fixture = tmp_path / "fixture.txt"
    fixture.write_text(canary, encoding="utf-8")
    original = secret_canary_scan._path_is_reparse_point

    def fake_reparse(path: Path) -> bool:
        if path == fixture:
            return True
        return original(path)

    monkeypatch.setattr(secret_canary_scan, "_path_is_reparse_point", fake_reparse)

    report = secret_canary_scan.scan_secret_canary(
        tmp_path,
        canary,
        fixture_input=fixture,
    )

    assert report.status == "INCOMPLETE"
    assert report.exit_code == 3
    assert report.errors[0].error_type == "FixtureReparsePoint"


def test_symlink_ancestor_of_root_fails_closed_before_scan(tmp_path: Path) -> None:
    canary = "planted-secret"
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    nested_root = real_parent / "scan-root"
    nested_root.mkdir()
    (nested_root / "safe.txt").write_text("safe", encoding="utf-8")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real_parent, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable in this test environment")

    report = secret_canary_scan.scan_secret_canary(alias / "scan-root", canary)

    assert report.status == "INCOMPLETE"
    assert report.exit_code == 3
    assert report.scanned_files == 0
    assert report.findings == ()
    assert report.errors[0].error_type == "RootSymlinkAncestor"


def test_reparse_ancestor_of_root_fails_closed_before_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    canary = "planted-secret"
    parent = tmp_path / "junction-parent"
    parent.mkdir()
    nested_root = parent / "scan-root"
    nested_root.mkdir()
    (nested_root / "safe.txt").write_text("safe", encoding="utf-8")
    original = secret_canary_scan._path_is_reparse_point

    def fake_reparse(path: Path) -> bool:
        if path == parent:
            return True
        return original(path)

    monkeypatch.setattr(secret_canary_scan, "_path_is_reparse_point", fake_reparse)

    report = secret_canary_scan.scan_secret_canary(nested_root, canary)

    assert report.status == "INCOMPLETE"
    assert report.exit_code == 3
    assert report.scanned_files == 0
    assert report.findings == ()
    assert report.errors[0].error_type == "RootReparsePointAncestor"
