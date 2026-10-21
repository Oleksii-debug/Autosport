"""Producer and independent verifier must use one ZIP resource envelope."""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport import release_package


def test_zip_writer_rejects_oversized_member_before_output_publication(tmp_path: Path) -> None:
    output = tmp_path / "release.zip"
    with patch.object(release_package, "_ZIP_MAX_MEMBER_UNCOMPRESSED", 64):
        with pytest.raises(ValueError, match="unsafe member size"):
            release_package._write_canonical_zip(output, {"Autosport-V1/item.bin": b"a" * 65})
    assert not output.exists()


def test_zip_writer_failure_does_not_replace_preexisting_output(tmp_path: Path) -> None:
    output = tmp_path / "release.zip"
    output.write_bytes(b"original owner bytes")
    with patch.object(release_package, "_ZIP_MAX_MEMBERS", 1):
        with pytest.raises(ValueError, match="excessive member count"):
            release_package._write_canonical_zip(output, {
                "Autosport-V1/one.bin": b"one", "Autosport-V1/two.bin": b"two",
            })
    assert output.read_bytes() == b"original owner bytes"


def test_zip_writer_rejects_unsafe_ratio_before_publication(tmp_path: Path) -> None:
    output = tmp_path / "release.zip"
    with patch.object(release_package, "_ZIP_MAX_COMPRESSION_RATIO", 2):
        with pytest.raises(ValueError, match="excessive compression ratio"):
            release_package._write_canonical_zip(output, {"Autosport-V1/repeated.bin": b"z" * 2000})
    assert not output.exists()


def test_zip_writer_publishes_valid_canonical_small_archive(tmp_path: Path) -> None:
    output = tmp_path / "release.zip"
    expected = b"ordinary release payload"
    sha = release_package._write_canonical_zip(output, {"Autosport-V1/item.bin": expected})
    assert output.is_file()
    assert sha == hashlib.sha256(output.read_bytes()).hexdigest()
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["Autosport-V1/item.bin"]
        release_package._require_release_zip_resource_bounds(archive.infolist())
        assert archive.read("Autosport-V1/item.bin") == expected
