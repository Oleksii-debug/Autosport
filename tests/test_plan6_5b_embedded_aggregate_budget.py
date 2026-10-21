"""Plan 6 §5Б: embedded CArchive size must be bounded in aggregate.

Both the main EXE and Data EXE invoke the same validator. The enclosing ZIP
byte budget alone cannot bound the decompressed PyInstaller members.
This is a scoped source qualification, not a bundled-wheel SBOM or release PASS.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from autosport.release_sbom import validate_embedded_executable_inventory


SOURCE = "a" * 40
EXE = "b" * 64


def inventory(first_packed: int, second_packed: int,
              first_unpacked: int, second_unpacked: int) -> dict:
    return {
        "schema_version": 1,
        "source_sha": SOURCE,
        "executable_sha256": EXE,
        "evidence_class": "EMBEDDED_PYINSTALLER_CARCHIVE_TOC_ONLY",
        "components": [
            {"name": "PYZ.pyz", "typecode": "z",
             "packed_bytes": first_packed, "unpacked_bytes": first_unpacked},
            {"name": "python312.dll", "typecode": "b",
             "packed_bytes": second_packed, "unpacked_bytes": second_unpacked},
        ],
    }


@pytest.mark.parametrize("payload", [
    inventory(600_000_000, 600_000_000, 2, 2),
    inventory(100, 100, 3 << 30, 3 << 30),
])
def test_embedded_executable_rejects_aggregate_archive_bomb(payload: dict) -> None:
    # Every individual member is under the preexisting 2**35 byte bound.
    # Only the aggregate compressed/unpacked resource budget detects this.
    with pytest.raises(ValueError, match="archive size budget exceeded"):
        validate_embedded_executable_inventory(
            deepcopy(payload), source_sha=SOURCE, executable_sha256=EXE,
        )


@pytest.mark.parametrize("payload", [
    inventory(1 << 29, 1 << 29, 1 << 31, 1 << 31),
    inventory(0, 0, 0, 0),
])
def test_embedded_executable_accepts_aggregate_budget_boundary(payload: dict) -> None:
    validate_embedded_executable_inventory(
        payload, source_sha=SOURCE, executable_sha256=EXE,
    )


def test_aggregate_budget_is_shared_by_both_release_verifiers() -> None:
    # The production and Data-tool ZIP verifiers must not bypass the common
    # CArchive validator when their own manifests and digests are resealed.
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for file_name in ("release_package.py", "data_tool_package.py"):
        source = (root / "src" / "autosport" / file_name).read_text("utf-8")
        assert "validate_embedded_executable_inventory(" in source
