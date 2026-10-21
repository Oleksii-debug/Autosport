"""Plan 6 §5В: fixture-only detached release-signing primitive, never production trust."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fixture_cms_signing.ps1"
TEXT = SCRIPT.read_text(encoding="utf-8")
WINDOWS = sys.platform == "win32"


def _invoke(package: Path) -> subprocess.CompletedProcess[str]:
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    return subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), "-PackagePath", str(package)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )


def test_source_limits_fixture_to_ephemeral_memory_only_signature() -> None:
    assert "CreateSelfSigned(" in TEXT
    assert "SignedCms]::new(" in TEXT
    assert "ComputeSignature($signer)" in TEXT
    assert "CheckSignature($true)" in TEXT
    assert "PinnedThumbprint" in TEXT
    assert "tampered_content_rejected = $tamperRejected" in TEXT
    assert "wrong_certificate_pin_rejected = $wrongPinRejected" in TEXT
    assert "source_archive_unchanged = $true" in TEXT
    assert "production_certificate_trusted = $false" in TEXT
    assert "final_release_signed = $false" in TEXT
    assert "real_money_execution = $false" in TEXT
    assert "human_tested = $false" in TEXT
    assert "nvda_verified = $false" in TEXT
    assert "268435456" in TEXT
    assert "ReadAllBytes($path)" in TEXT
    assert "certificate.Export(" not in TEXT
    assert "Export-PfxCertificate" not in TEXT
    assert "Import-Certificate" not in TEXT
    assert "TrustedPublisher" not in TEXT
    assert "Set-AuthenticodeSignature" not in TEXT


@pytest.mark.skipif(not WINDOWS, reason="detached CMS Windows fixture requires PowerShell/UIA qualification runner")
def test_positive_fixture_rejects_tamper_and_wrong_signer_without_mutating_archive(tmp_path: Path) -> None:
    archive = tmp_path / "Autosport-test-release.zip"
    payload = b"Autosport public signing fixture; never production trust.\\n"
    archive.write_bytes(payload)
    result = _invoke(archive)
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert len(rows) == 1, result.stdout
    report = json.loads(rows[0])
    assert report["status"] == "PASS"
    assert report["evidence_class"] == "FIXTURE_DETACHED_CMS_ONLY"
    assert report["archive_sha256"] == hashlib.sha256(payload).hexdigest()
    assert report["signature_algorithm"] == "CMS_SHA256_RSA3072_PKCS1"
    assert report["mathematical_signature_verified"] is True
    assert report["tampered_content_rejected"] is True
    assert report["wrong_certificate_pin_rejected"] is True
    assert report["source_archive_unchanged"] is True
    assert report["production_certificate_trusted"] is False
    assert report["final_release_signed"] is False
    assert report["real_money_execution"] is False
    assert report["human_tested"] is False
    assert report["nvda_verified"] is False
    assert archive.read_bytes() == payload


@pytest.mark.skipif(not WINDOWS, reason="fixture archive refusal requires Windows PowerShell")
@pytest.mark.parametrize("case", ["missing", "empty", "oversize"])
def test_invalid_fixture_inputs_fail_closed(tmp_path: Path, case: str) -> None:
    archive = tmp_path / ("package-" + case + ".zip")
    if case == "empty":
        archive.write_bytes(b"")
    elif case == "oversize":
        with archive.open("wb") as handle:
            handle.truncate(268435457)
    result = _invoke(archive)
    assert result.returncode != 0
    assert '"status":"PASS"' not in result.stdout


def test_windows_candidate_invokes_fixture_against_exact_candidate_archive() -> None:
    workflow = (ROOT / ".github" / "workflows" / "windows-build.yml").read_text(encoding="utf-8")
    assert "Qualify release CMS signing with ephemeral fixture" in workflow
    assert "./scripts/fixture_cms_signing.ps1 -PackagePath ./dist/Autosport-V1-windows-x64.zip" in workflow
