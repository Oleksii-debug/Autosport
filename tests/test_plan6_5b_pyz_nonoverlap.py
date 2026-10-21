"""Plan 6 §5Б: nested PYZ evidence must represent non-overlapping bytes.

Source/fixture validation only; not a native Windows build, bundled wheel
attribution, third-party license audit, or §5Б terminal qualification.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from autosport.release_sbom import validate_embedded_executable_inventory

SOURCE = "a" * 40
EXE = "b" * 64


def _inventory(first: tuple[int, int], second: tuple[int, int]) -> dict:
    return {
        "schema_version": 2,
        "source_sha": SOURCE,
        "executable_sha256": EXE,
        "evidence_class": "EMBEDDED_PYINSTALLER_CARCHIVE_AND_PYZ_TOC_ONLY",
        "components": [
            {"name": "PYZ.pyz", "typecode": "z",
             "packed_bytes": 4096, "unpacked_bytes": 4096},
            {"name": "python312.dll", "typecode": "b",
             "packed_bytes": 128, "unpacked_bytes": 128},
        ],
        "pyz_modules": [
            {"name": "autosport.alpha", "typecode": 0,
             "position": first[0], "packed_bytes": first[1]},
            {"name": "autosport.beta", "typecode": 0,
             "position": second[0], "packed_bytes": second[1]},
        ],
    }


@pytest.mark.parametrize("first,second", [
    ((16, 128), (80, 128)),   # partially overlapping compressed records
    ((16, 256), (40, 16)),   # one record fully inside the other
    ((16, 128), (16, 128)),  # duplicate extents under different names
    ((80, 128), (16, 128)),  # TOC is name-sorted, not offset-sorted
])
def test_overlapping_pyz_extents_are_rejected(first, second):
    payload = _inventory(first, second)
    with pytest.raises(ValueError, match="PYZ module ranges overlap"):
        validate_embedded_executable_inventory(
            deepcopy(payload), source_sha=SOURCE, executable_sha256=EXE,
        )


@pytest.mark.parametrize("first,second", [
    ((16, 128), (144, 128)),  # adjacent ranges, no overlap
    ((16, 128), (512, 128)),  # distinct, nonadjacent ranges
    ((16, 0), (16, 128)),    # zero-byte metadata is not an interval
    ((16, 128), (144, 0)),   # zero-byte metadata at boundary
])
def test_disjoint_and_empty_pyz_extents_remain_valid(first, second):
    validate_embedded_executable_inventory(
        _inventory(first, second), source_sha=SOURCE, executable_sha256=EXE,
    )
