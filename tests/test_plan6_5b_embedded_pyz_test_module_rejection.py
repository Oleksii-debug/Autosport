"""Plan 6 5Б: no test packages hidden inside the sealed PyInstaller PYZ.

The outer CArchive member safety checks are insufficient for nested modules.
This tests the same validator used for both shipped Windows executables.
"""
from __future__ import annotations

import pytest

from autosport.release_sbom import validate_embedded_executable_inventory


_SOURCE = "a" * 40
_EXE = "b" * 64


def _inventory(name: str) -> dict:
    return {
        "schema_version": 2,
        "source_sha": _SOURCE,
        "executable_sha256": _EXE,
        "evidence_class": "EMBEDDED_PYINSTALLER_CARCHIVE_AND_PYZ_TOC_ONLY",
        "components": [{
            "name": "PYZ-00.pyz",
            "typecode": "z",
            "packed_bytes": 32,
            "unpacked_bytes": 1024,
        }],
        "pyz_modules": [{
            "name": name,
            "typecode": 0,
            "position": 8,
            "packed_bytes": 32,
        }],
    }


@pytest.mark.parametrize("module_name", [
    "test",
    "tests",
    "autosport.tests",
    "autosport.test.helpers",
    "pytest",
    "autosport.pytest",
    "hypothesis",
    "autosport.hypothesis.strategies",
    "nox",
    "autosport.nox",
    "TESTS",
])
def test_nested_pyz_test_modules_are_not_release_payload(module_name: str) -> None:
    with pytest.raises(ValueError, match="nested PYZ contains non-release test modules"):
        validate_embedded_executable_inventory(
            _inventory(module_name), source_sha=_SOURCE, executable_sha256=_EXE,
        )


@pytest.mark.parametrize("module_name", [
    "autosport.release_sbom",
    "autosport.contest",
    "autosport.hypothesis_engine",
    "autosport.pytesting",
    "unittest",
])
def test_unrelated_names_and_runtime_modules_remain_permitted(module_name: str) -> None:
    validate_embedded_executable_inventory(
        _inventory(module_name), source_sha=_SOURCE, executable_sha256=_EXE,
    )


def test_forged_test_entry_cannot_hide_among_valid_sorted_modules() -> None:
    payload = _inventory("autosport.core")
    payload["pyz_modules"] = [
        dict(payload["pyz_modules"][0]),
        dict(payload["pyz_modules"][0], name="autosport.tests", position=64),
    ]
    with pytest.raises(ValueError, match="nested PYZ contains non-release test modules"):
        validate_embedded_executable_inventory(
            payload, source_sha=_SOURCE, executable_sha256=_EXE,
        )
