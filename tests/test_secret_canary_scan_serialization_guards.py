from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import pytest

import autosport.secret_canary_scan as secret_canary_scan


@pytest.mark.parametrize(
    "variant",
    (
        "standard",
        "standard-unpadded",
        "urlsafe",
        "urlsafe-unpadded",
    ),
)
def test_reversible_direct_base64_cannot_hide_canary(
    tmp_path: Path,
    variant: str,
) -> None:
    canary = "oauth-S3/😀-А/+_secret"
    raw = canary.encode("utf-8")
    if variant.startswith("urlsafe"):
        payload = base64.urlsafe_b64encode(raw)
    else:
        payload = base64.b64encode(raw)
    if variant.endswith("unpadded"):
        payload = payload.rstrip(b"=")
    assert raw not in payload

    (tmp_path / "credential-cache.txt").write_bytes(
        b"token_b64=" + payload + b"\n"
    )

    report = secret_canary_scan.scan_secret_canary(
        tmp_path,
        canary,
        chunk_size=3,
    )

    assert report.status == "LEAK"
    assert report.exit_code == 2
    assert len(report.findings) == 1
    assert f"base64-{variant}" in report.findings[0].encodings
    assert canary not in repr(report)


def test_basic_authorization_base64_cannot_hide_embedded_canary_across_chunks(
    tmp_path: Path,
) -> None:
    canary = "S3/😀-А-secret"
    raw = canary.encode("utf-8")
    payload = base64.b64encode(b"user:" + raw)
    assert raw not in payload
    assert base64.b64encode(raw) not in payload

    (tmp_path / "http-diagnostic.txt").write_bytes(
        b"Authorization: Basic " + payload + b"\r\n"
    )

    report = secret_canary_scan.scan_secret_canary(
        tmp_path,
        canary,
        chunk_size=5,
    )

    assert report.status == "LEAK"
    assert report.exit_code == 2
    assert len(report.findings) == 1
    assert "base64-semantic" in report.findings[0].encodings
    assert canary not in repr(report)


def test_urlsafe_unpadded_base64_wrapper_cannot_hide_embedded_canary(
    tmp_path: Path,
) -> None:
    canary = "url-safe-credential-😀"
    raw = canary.encode("utf-8")
    payload = base64.urlsafe_b64encode(b"p:" + raw + b":s").rstrip(b"=")
    assert raw not in payload
    assert base64.urlsafe_b64encode(raw).rstrip(b"=") not in payload

    (tmp_path / "session-token.txt").write_bytes(b"session=" + payload + b"\n")

    report = secret_canary_scan.scan_secret_canary(
        tmp_path,
        canary,
        chunk_size=4,
    )

    assert report.status == "LEAK"
    assert report.exit_code == 2
    assert "base64-semantic" in report.findings[0].encodings
    assert canary not in repr(report)


def test_line_wrapped_base64_cannot_hide_canary_across_chunks(
    tmp_path: Path,
) -> None:
    canary = "secret-canary-0123456789"
    raw = canary.encode("utf-8")
    # 56 bytes before the canary place the MIME 76-column fold inside the
    # canary's encoded span, so neither individual Base64 line contains the
    # complete decoded secret.
    payload = base64.encodebytes(b"A" * 55 + b":" + raw + b":tail")
    assert raw not in payload
    assert b"\n" in payload
    assert base64.b64encode(raw) not in payload

    (tmp_path / "wrapped-auth-diagnostic.txt").write_bytes(payload)

    report = secret_canary_scan.scan_secret_canary(
        tmp_path,
        canary,
        chunk_size=7,
    )

    assert report.status == "LEAK"
    assert report.exit_code == 2
    assert len(report.findings) == 1
    assert "base64-semantic" in report.findings[0].encodings
    assert canary not in repr(report)


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_folded_base64_real_newlines_not_mistaken_for_literal_slashes(
    tmp_path: Path,
    line_ending: bytes,
) -> None:
    canary = "folded-only-canary-CRLF-0123456789"
    raw = canary.encode("utf-8")
    encoded = base64.b64encode(b"prefix:" + raw + b":suffix")
    folded = line_ending.join(
        encoded[index : index + 12]
        for index in range(0, len(encoded), 12)
    )
    assert raw not in folded
    assert base64.b64encode(raw) not in folded
    assert line_ending in folded
    (tmp_path / "folded-credential.log").write_bytes(folded)

    report = secret_canary_scan.scan_secret_canary(
        tmp_path,
        canary,
        chunk_size=7,
    )

    assert report.status == "LEAK"
    assert report.exit_code == 2
    assert len(report.findings) == 1
    assert "base64-semantic" in report.findings[0].encodings
    assert canary not in repr(report)


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

    assert report.status == "LEAK", tuple(error.error_type for error in report.errors)
    assert report.exit_code == 2
    assert len(report.findings) == 1
    expected = "json-string-ascii" if ensure_ascii else "json-string-utf8"
    assert expected in report.findings[0].encodings
    assert canary not in repr(report)


def _unicode_escape_every_code_unit(value: str) -> bytes:
    encoded: list[str] = []
    for character in value:
        code_point = ord(character)
        if code_point <= 0xFFFF:
            encoded.append(f"\\u{code_point:04x}")
            continue
        scalar = code_point - 0x10000
        high = 0xD800 + (scalar >> 10)
        low = 0xDC00 + (scalar & 0x3FF)
        encoded.append(f"\\u{high:04X}\\u{low:04x}")
    return "".join(encoded).encode("ascii")


def test_json_semantic_unicode_escapes_cannot_hide_canary_across_chunks(
    tmp_path: Path,
) -> None:
    canary = "S3/😀-А-secret"
    payload = _unicode_escape_every_code_unit(canary)
    assert canary.encode("utf-8") not in payload
    assert json.dumps(canary, ensure_ascii=True)[1:-1].encode("ascii") != payload
    (tmp_path / "unicode-escaped.json").write_bytes(
        b'{"token":"' + payload + b'"}'
    )

    report = secret_canary_scan.scan_secret_canary(
        tmp_path,
        canary,
        chunk_size=4,
    )

    assert report.status == "LEAK"
    assert report.exit_code == 2
    assert len(report.findings) == 1
    assert "json-string-semantic" in report.findings[0].encodings
    assert canary not in repr(report)


def test_json_semantic_mixed_simple_and_solidus_escapes_cannot_hide_canary(
    tmp_path: Path,
) -> None:
    canary = 'oauth/"\\-line\n-secret'
    canonical = json.dumps(canary, ensure_ascii=False)[1:-1]
    payload = canonical.replace("/", "\\/").encode("utf-8")
    assert payload != canonical.encode("utf-8")
    assert canary.encode("utf-8") not in payload
    (tmp_path / "mixed-escaped.json").write_bytes(
        b'{"token":"' + payload + b'"}'
    )

    report = secret_canary_scan.scan_secret_canary(
        tmp_path,
        canary,
        chunk_size=3,
    )

    assert report.status == "LEAK"
    assert report.exit_code == 2
    assert "json-string-semantic" in report.findings[0].encodings
    assert canary not in repr(report)


def test_malformed_surrogate_sequence_does_not_fabricate_json_semantic_match(
    tmp_path: Path,
) -> None:
    canary = "A😀B"
    (tmp_path / "malformed.json").write_bytes(
        b'{"token":"A\\uD83DX\\uDE00B"}'
    )

    report = secret_canary_scan.scan_secret_canary(
        tmp_path,
        canary,
        chunk_size=5,
    )

    assert report.status == "CLEAN", tuple(error.error_type for error in report.errors)
    assert report.exit_code == 0
    assert report.findings == ()


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
    junction_identity = secret_canary_scan._identity_from_stat(junction.lstat())
    original = secret_canary_scan._metadata_is_reparse_point

    def fake_reparse(metadata: os.stat_result) -> bool:
        identity = secret_canary_scan._identity_from_stat(metadata)
        if secret_canary_scan._same_object_identity(identity, junction_identity):
            return True
        return original(metadata)

    monkeypatch.setattr(
        secret_canary_scan,
        "_metadata_is_reparse_point",
        fake_reparse,
    )

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


def test_file_replacement_immediately_before_open_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    canary = "planted-secret"
    victim = tmp_path / "artifact.bin"
    victim.write_bytes(b"safe")
    replacement = tmp_path / "replacement.bin"
    replacement.write_text(canary, encoding="utf-8")
    original_open = secret_canary_scan._open_readonly_no_follow
    swapped = False

    def swap_then_open(path: Path) -> int:
        nonlocal swapped
        if path == victim and not swapped:
            os.replace(replacement, victim)
            swapped = True
        return original_open(path)

    monkeypatch.setattr(
        secret_canary_scan,
        "_open_readonly_no_follow",
        swap_then_open,
    )

    report = secret_canary_scan.scan_secret_canary(tmp_path, canary)

    assert swapped is True
    assert report.status == "INCOMPLETE"
    assert report.exit_code == 3
    assert any(error.error_type == "FileIdentityChanged" for error in report.errors)


def test_fixture_replacement_after_validation_is_not_silently_excluded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    canary = "planted-secret"
    fixture = tmp_path / "fixture.txt"
    fixture.write_text(canary, encoding="utf-8")
    replacement = tmp_path / "replacement.txt"
    replacement.write_text(canary, encoding="utf-8")
    original_scan_directory = secret_canary_scan._scan_directory
    swapped = False

    def swap_then_scan(
        path: Path,
        expected_identity: secret_canary_scan._PathIdentity,
    ):
        nonlocal swapped
        if path == tmp_path and not swapped:
            os.replace(replacement, fixture)
            swapped = True
        return original_scan_directory(path, expected_identity)

    monkeypatch.setattr(secret_canary_scan, "_scan_directory", swap_then_scan)

    report = secret_canary_scan.scan_secret_canary(
        tmp_path,
        canary,
        fixture_input=fixture,
    )

    assert swapped is True
    assert report.status == "INCOMPLETE"
    assert report.exit_code == 3
    assert report.excluded_files == 0
    # The parent directory may reject the rename before fixture identity is
    # checked; both fail closed without excluding the substituted fixture.
    assert any(
        error.error_type in {"DirectoryIdentityChanged", "FixtureIdentityChanged"}
        for error in report.errors
    )


def test_directory_replacement_before_descent_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    canary = "planted-secret"
    child = tmp_path / "child"
    child.mkdir()
    (child / "safe.txt").write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(canary, encoding="utf-8")
    original_scan_directory = secret_canary_scan._scan_directory
    swapped = False

    def swap_child_then_scan(
        path: Path,
        expected_identity: secret_canary_scan._PathIdentity,
    ):
        nonlocal swapped
        if path == child and not swapped:
            backup = tmp_path / "child-original"
            child.rename(backup)
            try:
                child.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                backup.rename(child)
                pytest.skip("directory symlinks unavailable in this test environment")
            swapped = True
        return original_scan_directory(path, expected_identity)

    monkeypatch.setattr(secret_canary_scan, "_scan_directory", swap_child_then_scan)

    report = secret_canary_scan.scan_secret_canary(tmp_path, canary)

    assert swapped is True
    assert report.status == "INCOMPLETE"
    assert report.exit_code == 3
    assert any(
        error.error_type == "DirectoryIdentityChanged"
        for error in report.errors
    )




def test_late_file_created_after_directory_snapshot_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    canary = "planted-secret"
    original_scan_directory = secret_canary_scan._scan_directory
    injected = False

    def scan_then_inject(
        path: Path,
        expected_identity: secret_canary_scan._PathIdentity,
    ):
        nonlocal injected
        entries = original_scan_directory(path, expected_identity)
        if path == tmp_path and not injected:
            (tmp_path / "late-secret.txt").write_text(canary, encoding="utf-8")
            injected = True
        return entries

    monkeypatch.setattr(
        secret_canary_scan,
        "_scan_directory",
        scan_then_inject,
    )

    report = secret_canary_scan.scan_secret_canary(tmp_path, canary)

    assert injected is True
    assert report.status == "INCOMPLETE"
    assert report.exit_code == 3
    assert report.findings == ()
    assert any(
        error.error_type == "DirectoryIdentityChanged"
        for error in report.errors
    )


def test_file_mutated_after_bound_read_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    canary = "planted-secret"
    victim = tmp_path / "artifact.bin"
    victim.write_bytes(b"safe")
    original_scan_file = secret_canary_scan._scan_file
    mutated = False

    def scan_then_mutate(*args, **kwargs):
        nonlocal mutated
        result = original_scan_file(*args, **kwargs)
        path = args[0]
        if path == victim and not mutated:
            path.write_text(canary, encoding="utf-8")
            mutated = True
        return result

    monkeypatch.setattr(secret_canary_scan, "_scan_file", scan_then_mutate)

    report = secret_canary_scan.scan_secret_canary(tmp_path, canary)

    assert mutated is True
    assert report.status == "INCOMPLETE"
    assert report.exit_code == 3
    assert report.findings == ()
    assert any(
        error.error_type == "FileIdentityChanged"
        for error in report.errors
    )


def test_symlink_swap_immediately_before_open_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    canary = "planted-secret"
    victim = tmp_path / "artifact.bin"
    victim.write_bytes(b"safe")
    target = tmp_path.parent / f"{tmp_path.name}-outside-target.bin"
    target.write_text(canary, encoding="utf-8")
    original_open = secret_canary_scan._open_readonly_no_follow
    swapped = False

    def swap_to_symlink_then_open(path: Path) -> int:
        nonlocal swapped
        if path == victim and not swapped:
            victim.unlink()
            try:
                victim.symlink_to(target)
            except (OSError, NotImplementedError):
                victim.write_bytes(b"safe")
                pytest.skip("file symlinks unavailable in this test environment")
            swapped = True
        return original_open(path)

    monkeypatch.setattr(
        secret_canary_scan,
        "_open_readonly_no_follow",
        swap_to_symlink_then_open,
    )

    report = secret_canary_scan.scan_secret_canary(tmp_path, canary)

    assert swapped is True
    assert report.status == "INCOMPLETE"
    assert report.exit_code == 3
    assert report.findings == ()


def test_unbroken_base64_alphabet_has_bounded_scan_time(tmp_path: Path) -> None:
    """An unbroken Base64-like log line must not trigger quadratic regex misses."""

    canary = "planted-credential-Ж-0123456789"
    (tmp_path / "long-base64-like-log.txt").write_bytes(
        b"A" * 64_000 + b"\n!"
    )
    started = time.perf_counter()
    report = secret_canary_scan.scan_secret_canary(
        tmp_path, canary, chunk_size=64 * 1024
    )
    elapsed = time.perf_counter() - started

    assert report.status == "CLEAN"
    assert report.exit_code == 0
    assert not report.findings and not report.errors
    assert elapsed < 8.0, f"Base64 scan exceeded safe bounded time: {elapsed:.2f}s"
