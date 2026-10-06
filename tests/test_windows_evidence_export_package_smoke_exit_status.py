from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE = REPO_ROOT / "scripts" / "evidence_export_package_smoke.ps1"


def test_packaged_evidence_smoke_normalizes_success_exit_after_negative_probe() -> None:
    text = SMOKE.read_text(encoding="utf-8")

    negative_probe = "$negative = Invoke-DataTool"
    pass_marker = 'Write-Host "evidence_export_package_smoke=PASS'
    finally_marker = "} finally {"
    success_exit = "exit 0"

    for fragment in (negative_probe, pass_marker, finally_marker, success_exit):
        assert fragment in text

    assert text.index(negative_probe) < text.index(pass_marker)
    assert text.index(pass_marker) < text.index(finally_marker)
    assert text.index(finally_marker) < text.rindex(success_exit)
    assert text.rstrip().endswith(success_exit)
