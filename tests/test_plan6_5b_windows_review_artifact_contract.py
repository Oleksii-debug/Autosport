from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/windows-build.yml"


def test_exact_windows_build_uploads_binary_and_license_review_inputs_before_uia() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    upload = source.index("Upload exact executable SBOM and license review inputs")
    uia = source.index("External UIA fresh-extraction gate")
    assert upload < uia
    for relative in (
        "DEPENDENCY_GRAPH_METADATA.json",
        "DEPENDENCY_LICENSE_EVIDENCE.json",
        "EMBEDDED_EXECUTABLE_INVENTORY.json",
        "BINARY_MODULE_ATTRIBUTION.json",
        "DATA_EXECUTABLE_INVENTORY.json",
        "DATA_BINARY_MODULE_ATTRIBUTION.json",
        "licenses/**",
    ):
        assert f".build-fresh-extraction/Autosport-V1/{relative}" in source
    block = source[upload:uia]
    assert "if-no-files-found: error" in block
