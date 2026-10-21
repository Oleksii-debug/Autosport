"""Exact binary-bound PYZ module census regression checks (Plan 6 Section 5B)."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import pytest

from autosport.release_sbom import (
    capture_embedded_executable_inventory,
    validate_embedded_executable_inventory,
)

SHA = "a" * 40


class Reader:
    toc = {
        "PYZ.pyz": (0, 4096, 4096, False, "z"),
        "python312.dll": (4096, 128, 128, False, "b"),
    }
    modules = {
        "__future__": (0, 16, 128),
        "autosport.release_sbom": (1, 144, 256),
    }

    def __init__(self, path: str):
        self.path = path

    def open_embedded_archive(self, name: str):
        assert name == "PYZ.pyz"
        return SimpleNamespace(toc=self.modules)


def _fixture(tmp_path: Path) -> Path:
    path = tmp_path / "Autosport.exe"
    path.write_bytes(b"test-binary" + bytes(8192))
    return path


def _captured(tmp_path: Path, *, reader=Reader):
    return capture_embedded_executable_inventory(
        _fixture(tmp_path), SHA, reader_factory=reader, include_nested_pyz=True,
    )


def test_nested_module_table_is_binary_bound(tmp_path: Path) -> None:
    result = _captured(tmp_path)
    assert result["schema_version"] == 2
    assert result["evidence_class"] == "EMBEDDED_PYINSTALLER_CARCHIVE_AND_PYZ_TOC_ONLY"
    assert [item["name"] for item in result["pyz_modules"]] == [
        "__future__", "autosport.release_sbom",
    ]
    validate_embedded_executable_inventory(
        result, source_sha=SHA, executable_sha256=result["executable_sha256"],
    )
    old = capture_embedded_executable_inventory(
        _fixture(tmp_path), SHA, reader_factory=Reader,
    )
    assert old["schema_version"] == 1
    assert "pyz_modules" not in old


@pytest.mark.parametrize("change", [
    lambda x: x.pop("pyz_modules"),
    lambda x: x.update(schema_version=True),
    lambda x: x["pyz_modules"].clear(),
    lambda x: x["pyz_modules"].append(deepcopy(x["pyz_modules"][-1])),
    lambda x: x["pyz_modules"].reverse(),
    lambda x: x["pyz_modules"][0].update(position=True),
    lambda x: x["pyz_modules"][0].update(position=4090, packed_bytes=40),
    lambda x: x["pyz_modules"][0].update(typecode=42),
    lambda x: x["pyz_modules"][0].update(name="not/a/module"),
    lambda x: x["components"][0].update(typecode="b"),
])
def test_malformed_nested_module_evidence_is_rejected(tmp_path: Path, change) -> None:
    result = _captured(tmp_path)
    forged = deepcopy(result)
    change(forged)
    with pytest.raises(ValueError):
        validate_embedded_executable_inventory(
            forged, source_sha=SHA, executable_sha256=result["executable_sha256"],
        )


def test_module_case_alias_is_not_unique_on_windows(tmp_path: Path) -> None:
    class AliasReader(Reader):
        modules = {
            "Autosport.runtime": (0, 16, 128),
            "autosport.runtime": (0, 144, 128),
        }

    with pytest.raises(ValueError, match="collide"):
        _captured(tmp_path, reader=AliasReader)


def test_unreadable_pyz_fails_closed(tmp_path: Path) -> None:
    class BrokenReader(Reader):
        def open_embedded_archive(self, name: str):
            raise ValueError("unreadable")

    with pytest.raises(ValueError, match="embedded PYZ"):
        _captured(tmp_path, reader=BrokenReader)


def test_packager_enables_nested_census_for_both_exes() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts/package_windows.py").read_text(encoding="utf-8")
    assert source.count("include_nested_pyz=True") == 2
    assert "data_executable_inventory=data_embedded_inventory" in source
    assert "embedded_executable_inventory=embedded_inventory" in source
