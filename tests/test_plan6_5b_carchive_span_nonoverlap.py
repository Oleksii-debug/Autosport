"""Plan 6 §5Б: exact sealed CArchive TOC spans must not alias compressed bytes.

Source/fixture qualification only: no assertion of a Windows build, bundled
third-party license clearance, or physical NVDA acceptance.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport.release_sbom import capture_embedded_executable_inventory

SOURCE_SHA = "a" * 40


class _Reader:
    toc = {
        "PYZ.pyz": (0, 4096, 4096, False, "z"),
        "python312.dll": (4096, 128, 128, False, "b"),
    }

    def __init__(self, path: str):
        self.path = path

    def open_embedded_archive(self, name: str):
        assert name == "PYZ.pyz"
        return SimpleNamespace(toc={
            "__future__": (0, 16, 128),
            "autosport.release_sbom": (1, 144, 256),
        })


def _capture(tmp_path: Path, reader: type[_Reader], *, nested: bool = True):
    executable = tmp_path / "Autosport.exe"
    executable.write_bytes(b"test-pe-fixture" + bytes(8192))
    return capture_embedded_executable_inventory(
        executable, SOURCE_SHA, reader_factory=reader,
        include_nested_pyz=nested,
    )


def test_adjacent_carchive_spans_are_accepted(tmp_path: Path) -> None:
    result = _capture(tmp_path, _Reader)
    assert result["schema_version"] == 2
    assert len(result["components"]) == 2


@pytest.mark.parametrize("second_start, second_length", [
    (4095, 128),  # One byte shared by two members
    (512, 128),  # Entire second member aliases the PYZ archive
    (0, 4096),  # Exactly the same compressed byte interval
])
@pytest.mark.parametrize("nested", [False, True])
def test_overlapping_carchive_members_fail_closed(
    tmp_path: Path, second_start: int, second_length: int, nested: bool,
) -> None:
    class OverlapReader(_Reader):
        toc = {
            "PYZ.pyz": (0, 4096, 4096, False, "z"),
            "python312.dll": (second_start, second_length, second_length, False, "b"),
        }

    with pytest.raises(ValueError, match="CArchive member byte ranges overlap"):
        _capture(tmp_path, OverlapReader, nested=nested)


def test_zero_length_metadata_member_is_not_false_overlap(tmp_path: Path) -> None:
    class EmptyReader(_Reader):
        toc = {
            "PYZ.pyz": (0, 4096, 4096, False, "z"),
            "python312.dll": (4096, 128, 128, False, "b"),
            "runtime-option": (512, 0, 0, False, "o"),
        }

    result = _capture(tmp_path, EmptyReader)
    assert len(result["components"]) == 3
