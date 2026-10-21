"""Plan 6 §5Б: only a sealed EXE CArchive census, never a claimed SBOM/license signoff."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport.release_sbom import (
    capture_embedded_executable_inventory,
    validate_embedded_executable_inventory,
)


SOURCE = "a" * 40


class Reader:
    def __init__(self, _path: str) -> None:
        self.toc = {
            "PYZ.pyz": (0, 1024, 4096, 0, "z"),
            "python312.dll": (1024, 512, 1024, 1, "b"),
            "pyi_rth_inspect": (1536, 100, 200, 1, "s"),
        }


def test_actual_executable_digest_and_bounded_archive_members(tmp_path: Path) -> None:
    exe = tmp_path / "Autosport.exe"
    exe.write_bytes(b"sealed-test-executable" + b"\0" * 4096)
    result = capture_embedded_executable_inventory(exe, SOURCE, reader_factory=Reader)
    assert result["source_sha"] == SOURCE
    assert result["evidence_class"] == "EMBEDDED_PYINSTALLER_CARCHIVE_TOC_ONLY"
    assert len(result["components"]) == 3
    assert [part["name"] for part in result["components"]] == sorted(
        part["name"] for part in result["components"]
    )
    assert all("license" not in str(item).lower() for item in result["components"])
    validate_embedded_executable_inventory(
        result, source_sha=SOURCE, executable_sha256=result["executable_sha256"],
    )
    exe.write_bytes(b"candidate-changed" + b"\0" * 4096)
    updated = capture_embedded_executable_inventory(exe, SOURCE, reader_factory=Reader)
    assert updated["executable_sha256"] != result["executable_sha256"]


def test_executable_mutation_during_toc_capture_fails_closed(tmp_path: Path) -> None:
    exe = tmp_path / "Autosport.exe"
    exe.write_bytes(b"candidate" + b"\0" * 4096)

    class SwappingReader(Reader):
        def __init__(self, path: str) -> None:
            super().__init__(path)
            # Same-length mutation after initial SHA read and while the archive
            # reader is enumerating records: TOC and final digest cannot mix.
            Path(path).write_bytes(b"candidaXe" + b"\0" * 4096)

    with pytest.raises(ValueError, match="changed during CArchive inventory"):
        capture_embedded_executable_inventory(
            exe, SOURCE, reader_factory=SwappingReader,
        )


@pytest.mark.parametrize("change", [
    lambda x: x.update(evidence_class="COMPLETE_SBOM_LICENSE_CLEARED"),
    lambda x: x.update(source_sha="b" * 40),
    lambda x: x.update(schema_version=True),
    lambda x: x["components"].append(deepcopy(x["components"][-1])),
    lambda x: x["components"].reverse(),
    lambda x: x["components"][0].update(name="../../private.key"),
    lambda x: x["components"][0].update(name="C:\\secret"),
    lambda x: x["components"][0].update(typecode="not-a-type"),
    lambda x: x["components"][0].update(packed_bytes=True),
    lambda x: x["components"][0].update(unpacked_bytes=-1),
    lambda x: x.update(components=[]),
])
def test_rejects_forged_unsafe_or_unbounded_archive_census(tmp_path: Path, change) -> None:
    exe = tmp_path / "Autosport.exe"
    exe.write_bytes(b"candidate" + b"\0" * 4096)
    captured = capture_embedded_executable_inventory(exe, SOURCE, reader_factory=Reader)
    tampered = deepcopy(captured)
    change(tampered)
    with pytest.raises(ValueError):
        validate_embedded_executable_inventory(
            tampered, source_sha=SOURCE, executable_sha256=captured["executable_sha256"],
        )


@pytest.mark.parametrize("toc", [
    {},
    {"x": (1, 2, 3, False)},
    {"x": (-1, 2, 3, False, "b")},
    {"x": (0, 2, 3, 2, "b")},
    {"x": (0, 2, 3, False, "b"), "y": (0, 1, 2, False)},
])
def test_malformed_carchive_is_rejected(tmp_path: Path, toc) -> None:
    exe = tmp_path / "Autosport.exe"
    exe.write_bytes(b"candidate" + b"\0" * 4096)
    with pytest.raises(ValueError):
        capture_embedded_executable_inventory(
            exe, SOURCE, reader_factory=lambda _: SimpleNamespace(toc=toc),
        )


@pytest.mark.parametrize("toc", [
    {"beyond_end": (4096, 100, 100, False, "b")},
    {"oversize": (1, 8192, 8192, True, "b")},
    {"bool_position": (True, 1, 1, False, "b")},
    {"bool_packed": (0, True, 1, False, "b")},
    {"bool_unpacked": (0, 1, True, False, "b")},
])
def test_carchive_cannot_advertise_data_outside_sealed_executable(
    tmp_path: Path, toc,
) -> None:
    exe = tmp_path / "Autosport.exe"
    exe.write_bytes(b"\0" * 4096)
    with pytest.raises(ValueError, match="malformed executable CArchive member flags"):
        capture_embedded_executable_inventory(
            exe, SOURCE, reader_factory=lambda _: SimpleNamespace(toc=toc),
        )


def test_real_package_path_must_capture_and_verify_embedded_census() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts/package_windows.py").read_text(encoding="utf-8")
    package = (root / "src/autosport/release_package.py").read_text(encoding="utf-8")
    assert "capture_embedded_executable_inventory(" in source
    assert "embedded_executable_inventory=embedded_inventory" in source
    assert "require_embedded_executable_inventory=True" in source
    assert '"EMBEDDED_EXECUTABLE_INVENTORY.json"' in package
    assert "validate_embedded_executable_inventory(" in package
