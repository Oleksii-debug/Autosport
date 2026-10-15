from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from autosport import release_package


_SOURCE_SHA = "a" * 40


@dataclass
class _NonCanonicalInfo:
    filename: str = "Autosport-V1/Autosport.exe"
    date_time: tuple[int, int, int, int, int, int] = release_package._FIXED_ZIP_TIME
    compress_type: int = release_package.zipfile.ZIP_STORED
    create_system: int = release_package._ZIP_CREATE_SYSTEM
    create_version: int = release_package._ZIP_CREATE_VERSION
    extract_version: int = release_package._ZIP_EXTRACT_VERSION
    reserved: int = release_package._ZIP_RESERVED
    flag_bits: int = 0
    volume: int = release_package._ZIP_VOLUME
    internal_attr: int = release_package._ZIP_INTERNAL_ATTR
    external_attr: int = 0o755 << 16
    extra: bytes = b""
    comment: bytes = b""

    def is_dir(self) -> bool:
        return False


class _RecordingArchive:
    comment = b""

    def __init__(self) -> None:
        self.info = _NonCanonicalInfo()
        self.read_calls: list[str] = []

    def __enter__(self) -> "_RecordingArchive":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def infolist(self) -> list[_NonCanonicalInfo]:
        return [self.info]

    def read(self, name: str) -> bytes:
        self.read_calls.append(name)
        raise AssertionError(
            "release verifier decoded a member payload before rejecting "
            "non-canonical ZIP metadata"
        )


def test_noncanonical_zip_metadata_is_rejected_before_payload_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate.zip"
    candidate.write_bytes(b"not-used-by-fake-archive")
    archive = _RecordingArchive()
    monkeypatch.setattr(
        release_package.zipfile,
        "ZipFile",
        lambda *_args, **_kwargs: archive,
    )

    with pytest.raises(ValueError, match="non-canonical compression method"):
        release_package.verify_windows_package(
            candidate,
            expected_source_sha=_SOURCE_SHA,
        )

    assert archive.read_calls == []
