"""Plan 6 §5Б: sealed embedded CArchive names cannot alias Windows paths.

This is an executable source/fixture regression, NOT a packaged Windows build,
complete transitive SBOM, license clearance, real-money or physical NVDA proof.
"""
from copy import deepcopy

import pytest

from autosport.release_sbom import validate_embedded_executable_inventory

SHA = "a" * 40
EXE = "b" * 64


def inventory(*names):
    return {
        "schema_version": 1,
        "source_sha": SHA,
        "executable_sha256": EXE,
        "evidence_class": "EMBEDDED_PYINSTALLER_CARCHIVE_TOC_ONLY",
        "components": [
            {"name": name, "typecode": "b", "packed_bytes": 1, "unpacked_bytes": 1}
            for name in sorted(names)
        ],
    }


def qualify(*names):
    validate_embedded_executable_inventory(
        inventory(*names), source_sha=SHA, executable_sha256=EXE,
    )


@pytest.mark.parametrize("names", [
    ("foo.dll", "FOO.DLL"),
    ("lib\\foo.dll", "LIB/foo.dll"),
    ("data/cafe\u0301.dll", "data/caf\u00e9.dll"),
])
def test_reject_windows_aliases_even_when_raw_toc_names_are_distinct(names):
    with pytest.raises(ValueError, match="Windows member names collide"):
        qualify(*names)


@pytest.mark.parametrize("name", [
    "CON.txt", "dir/COM1.dll", "dir/LPT2.txt", "aux",
    "dir/name.", "dir/name ", "bad?.dll", "dir/stream:metadata",
    "dir/hidden\\tvalue",
])
def test_reject_unsafe_embedded_windows_member_names(name):
    with pytest.raises(ValueError, match="Windows member name is unsafe"):
        qualify(name)


def test_legitimate_public_templates_and_distinct_modules_remain_accepted():
    qualify("PYZ.pyz", "base_library.zip", "lib/python311.dll", ".env.example")


def test_tampered_frozen_manifest_cannot_self_authorize_renamed_colliding_member():
    payload = inventory("SRC/one.py", "src/two.py")
    validate_embedded_executable_inventory(payload, source_sha=SHA, executable_sha256=EXE)
    tampered = deepcopy(payload)
    tampered["components"][1]["name"] = "src/ONE.py"
    tampered["components"].sort(key=lambda item: item["name"])
    with pytest.raises(ValueError, match="Windows member names collide"):
        validate_embedded_executable_inventory(
            tampered, source_sha=SHA, executable_sha256=EXE,
        )
