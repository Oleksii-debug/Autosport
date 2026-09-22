from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import quote

import pytest

from autosport.secret_canary_scan import scan_secret_canary


CANARY = "S3cr3t-АВТ-OAuth?/+= token"


def test_clean_tree_reports_only_canary_digest(tmp_path: Path) -> None:
    (tmp_path / "журнал із пробілами.txt").write_text(
        "безпечний журнал",
        encoding="utf-8",
    )

    report = scan_secret_canary(tmp_path, CANARY, chunk_size=7)

    assert report.status == "CLEAN"
    assert report.exit_code == 0
    assert report.canary_sha256 == hashlib.sha256(CANARY.encode("utf-8")).hexdigest()
    assert report.scanned_files == 1
    assert report.findings == ()
    assert report.errors == ()
    assert CANARY not in repr(report)


@pytest.mark.parametrize(
    ("encoding", "payload"),
    [
        ("utf-8", CANARY.encode("utf-8")),
        ("utf-16le", CANARY.encode("utf-16le")),
        ("utf-16be", CANARY.encode("utf-16be")),
        ("url-percent", quote(CANARY, safe="").encode("ascii")),
        (
            "url-percent-lower",
            re.sub(
                r"%[0-9A-F]{2}",
                lambda match: match.group(0).lower(),
                quote(CANARY, safe=""),
            ).encode("ascii"),
        ),
    ],
)
def test_detects_supported_secret_encodings(
    tmp_path: Path,
    encoding: str,
    payload: bytes,
) -> None:
    path = tmp_path / f"artifact-{encoding}.bin"
    path.write_bytes(b"prefix:" + payload + b":suffix")

    report = scan_secret_canary(tmp_path, CANARY, chunk_size=11)

    assert report.status == "LEAK"
    assert report.exit_code == 2
    assert len(report.findings) == 1
    assert report.findings[0].encodings
    assert CANARY not in repr(report)


def test_url_percent_lowercase_hex_does_not_lowercase_literal_ascii(
    tmp_path: Path,
) -> None:
    # Percent-hex digits are case-insensitive, but unescaped ASCII bytes are not.
    false_variant = quote(CANARY, safe="").lower().encode("ascii")
    (tmp_path / "case-sensitive-url.bin").write_bytes(false_variant)

    report = scan_secret_canary(tmp_path, CANARY)

    assert report.status == "CLEAN"
    assert report.exit_code == 0


def test_detects_mixed_case_url_percent_hex_across_chunks(tmp_path: Path) -> None:
    encoded = quote(CANARY, safe="")
    index = 0

    def mixed_hex(match: re.Match[str]) -> str:
        nonlocal index
        value = match.group(0).upper() if index % 2 == 0 else match.group(0).lower()
        index += 1
        return value

    mixed = re.sub(r"%[0-9A-F]{2}", mixed_hex, encoded)
    assert mixed != encoded
    assert mixed != re.sub(
        r"%[0-9A-F]{2}",
        lambda match: match.group(0).lower(),
        encoded,
    )
    (tmp_path / "mixed-percent.bin").write_bytes(
        b"prefix:" + mixed.encode("ascii") + b":suffix"
    )

    report = scan_secret_canary(tmp_path, CANARY, chunk_size=7)

    assert report.status == "LEAK"
    assert report.exit_code == 2
    assert len(report.findings) == 1
    assert "url-percent-utf8-lower" in report.findings[0].encodings
    assert CANARY not in repr(report)


def test_detects_match_crossing_stream_chunk_boundary(tmp_path: Path) -> None:
    raw = CANARY.encode("utf-8")
    (tmp_path / "boundary.bin").write_bytes(b"x" * 4 + raw + b"tail")

    report = scan_secret_canary(tmp_path, CANARY, chunk_size=5)

    assert report.status == "LEAK"
    assert report.exit_code == 2


def test_exact_regular_fixture_input_may_be_excluded(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture input.txt"
    fixture.write_text(CANARY, encoding="utf-8")
    (tmp_path / "generated.log").write_text("safe", encoding="utf-8")

    report = scan_secret_canary(tmp_path, CANARY, fixture_input=fixture)

    assert report.status == "CLEAN"
    assert report.excluded_files == 1
    assert report.scanned_files == 1


def test_exclusion_outside_root_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text(CANARY, encoding="utf-8")

    report = scan_secret_canary(root, CANARY, fixture_input=outside)

    assert report.status == "INCOMPLETE"
    assert report.exit_code == 3
    assert report.errors[0].error_type == "FixtureOutsideRoot"
    assert CANARY not in repr(report)


def test_missing_fixture_exclusion_fails_closed(tmp_path: Path) -> None:
    report = scan_secret_canary(
        tmp_path,
        CANARY,
        fixture_input="missing.txt",
    )

    assert report.status == "INCOMPLETE"
    assert report.exit_code == 3
    assert report.errors[0].error_type == "FixtureNotRegularFile"


def test_symlink_is_rejected_without_following(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text(CANARY, encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable in this test environment")

    report = scan_secret_canary(
        tmp_path,
        CANARY,
        fixture_input=target,
    )

    assert report.status == "INCOMPLETE"
    assert report.exit_code == 3
    assert any(error.error_type == "SymlinkRejected" for error in report.errors)


def test_symlink_fixture_exclusion_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text(CANARY, encoding="utf-8")
    link = tmp_path / "fixture-link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable in this test environment")

    report = scan_secret_canary(
        tmp_path,
        CANARY,
        fixture_input=link,
    )

    assert report.status == "INCOMPLETE"
    assert report.exit_code == 3
    assert report.errors[0].error_type == "FixtureSymlink"


def test_secret_in_filename_does_not_echo_in_report(tmp_path: Path) -> None:
    path = tmp_path / f"{CANARY}.txt"
    try:
        path.write_text(CANARY, encoding="utf-8")
    except OSError:
        pytest.skip("filesystem does not support the planted canary filename")

    report = scan_secret_canary(tmp_path, CANARY)

    assert report.status == "LEAK"
    assert CANARY not in repr(report)
    assert path.name not in repr(report)


@pytest.mark.parametrize("chunk_size", [0, -1, True, 1.5])
def test_invalid_chunk_size_is_rejected(
    tmp_path: Path,
    chunk_size: object,
) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        scan_secret_canary(
            tmp_path,
            CANARY,
            chunk_size=chunk_size,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("canary", ["", b"bytes"])
def test_invalid_canary_is_rejected(tmp_path: Path, canary: object) -> None:
    with pytest.raises(ValueError, match="canary"):
        scan_secret_canary(
            tmp_path,
            canary,  # type: ignore[arg-type]
        )
