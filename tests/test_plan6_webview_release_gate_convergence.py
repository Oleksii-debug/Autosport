"""Release convergence regression: WebView2 cannot erase accepted Plan 6 release gates.

Source-only guard. Actual Windows PE/ZIP/UIA proof remains the exact-head
Windows candidate workflow and final owner acceptance remains separate.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_webview2_workflow_keeps_accepted_fixture_release_signature() -> None:
    workflow = (ROOT / ".github/workflows/windows-build.yml").read_text(encoding="utf-8")
    assert "AUTOSPORT_QUALIFIED_PACKAGE_SHA256=$packageSha" in workflow
    assert "Qualify fixture-only certificate-backed release/update signature" in workflow
    assert "./scripts/qualify_fixture_release_signature.ps1" in workflow
    assert "Upload fixture-only release signature evidence" in workflow
    assert "dist/fixture-release-signature-evidence.json" in workflow
    assert workflow.index("Qualify fixture-only certificate-backed release/update signature") > workflow.index("AUTOSPORT_QUALIFIED_PACKAGE_SHA256=$packageSha")
    assert "External UIA fresh-extraction gate" in workflow
    assert "extracted_external_uia_live_region_event_status" in workflow


def test_webview2_build_preserves_exact_sbom_source_and_clean_env() -> None:
    script = (ROOT / "scripts/build_windows.ps1").read_text(encoding="utf-8")
    assert '"src/autosport/release_sbom.py"' in script
    assert "'src/autosport/release_sbom.py'" in script
    assert 'load_module("autosport.release_sbom", "src/autosport/release_sbom.py")' in script
    assert script.index('load_module("autosport.release_sbom"') < script.index('load_module("autosport.release_package"')
    assert "& $packagingPython -I -c $script:trustedPackageLauncher" in script
    assert "src/autosport/release_sbom.py src/autosport/data_tool_package.py" in script
    assert "$trustedWebAssetsSpec" in script
    assert "--add-data $trustedWebAssetsSpec" in script


def test_package_source_binds_release_metadata_and_safety_policy() -> None:
    package = (ROOT / "src/autosport/release_package.py").read_text(encoding="utf-8")
    cli = (ROOT / "scripts/package_windows.py").read_text(encoding="utf-8")
    sbom = (ROOT / "src/autosport/release_sbom.py").read_text(encoding="utf-8")
    assert "validate_direct_dependency_metadata" in package
    assert "DIRECT_DEPENDENCY_METADATA.json" in package
    assert "require_dependency_metadata: bool = False" in package
    assert "_require_safe_release_inputs" in package
    assert "capture_direct_installed_dependencies" in cli
    assert "require_dependency_metadata=True" in cli
    assert "capture_direct_installed_dependencies(" in sbom
