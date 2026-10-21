"""Source-hash/TOC binding for Plan 6 §5Б with a transient EXE path swap."""
from __future__ import annotations

from pathlib import Path

import pytest

from autosport.release_sbom import capture_embedded_executable_inventory

SOURCE = "a" * 40


def test_reader_cannot_attribute_temporary_foreign_toc_to_sealed_exe(tmp_path: Path) -> None:
    exe = tmp_path / "Autosport.exe"
    original = b"sealed-original" + b"\0" * 4096
    foreign = b"foreign-binary!" + b"\0" * 4096
    assert len(original) == len(foreign)
    exe.write_bytes(original)

    class Reader:
        def __init__(self, read_path: str) -> None:
            # The mutable release filename is briefly switched *during* the
            # independent reader open, then restored before the final SHA read.
            exe.write_bytes(foreign)
            try:
                member = "forged.pyz" if Path(read_path).read_bytes() == foreign else "verified.pyz"
            finally:
                exe.write_bytes(original)
            self.toc = {member: (0, 1, 1, False, "b")}

    census = capture_embedded_executable_inventory(exe, SOURCE, reader_factory=Reader)
    assert [part["name"] for part in census["components"]] == ["verified.pyz"]


def test_temp_sealed_snapshot_mutation_fails_closed(tmp_path: Path) -> None:
    exe = tmp_path / "Autosport.exe"
    exe.write_bytes(b"sealed-original" + b"\0" * 4096)

    class Reader:
        def __init__(self, read_path: str) -> None:
            Path(read_path).write_bytes(b"forged-binary" + b"\0" * 4096)
            self.toc = {"forged.pyz": (0, 1, 1, False, "b")}

    with pytest.raises(ValueError, match="changed during CArchive inventory"):
        capture_embedded_executable_inventory(exe, SOURCE, reader_factory=Reader)
