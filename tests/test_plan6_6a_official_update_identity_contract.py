from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_official_windows_packager_authors_canonical_update_identity() -> None:
    source = (ROOT / "scripts/package_windows.py").read_text(encoding="utf-8")
    call = source.split("output, base_digest = build_windows_package(", 1)[1].split(
        "binding = bind_portable_data_tool(", 1
    )[0]
    assert 'update_version="0.1.0"' in call
    assert "update_schema=1" in call


def test_stage_neutral_materializer_requires_preserved_update_identity() -> None:
    source = (ROOT / "scripts/materialize_stage_neutral_release.ps1").read_text(
        encoding="utf-8"
    )
    assert "'update_version'" in source
    assert "'update_schema'" in source
    assert "$evidence.update_version -ne '0.1.0'" in source
    assert "[int]$evidence.update_schema -ne 1" in source
