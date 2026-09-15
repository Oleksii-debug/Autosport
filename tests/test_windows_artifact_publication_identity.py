from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _ROOT / ".github" / "workflows" / "windows-build.yml"


def test_release_zip_publication_is_bound_to_qualified_digest_before_and_after_upload() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    qualified_digest_export = '"AUTOSPORT_QUALIFIED_PACKAGE_SHA256=$packageSha"'
    pre_upload_gate = "- name: Reprove release ZIP immediately before artifact publication"
    upload_step = "- name: Upload prehuman Windows candidate"
    upload_id = "id: upload_prehuman_candidate"
    exact_download = "artifact-ids: ${{ steps.upload_prehuman_candidate.outputs.artifact-id }}"
    post_upload_gate = "- name: Verify GitHub-stored release ZIP identity"

    for contract in (
        qualified_digest_export,
        pre_upload_gate,
        upload_step,
        upload_id,
        exact_download,
        post_upload_gate,
    ):
        assert contract in workflow

    assert (
        workflow.index(qualified_digest_export)
        < workflow.index(pre_upload_gate)
        < workflow.index(upload_step)
        < workflow.index(exact_download)
        < workflow.index(post_upload_gate)
    )
    assert "dist/Autosport-V1-windows-x64.zip" in workflow
    assert workflow.count("AUTOSPORT_QUALIFIED_PACKAGE_SHA256") >= 3


def test_pre_upload_gate_adversarially_rejects_replaced_package_probe() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    pre_upload_start = workflow.index(
        "- name: Reprove release ZIP immediately before artifact publication"
    )
    upload_start = workflow.index("- name: Upload prehuman Windows candidate")
    gate = workflow[pre_upload_start:upload_start]

    assert "autosport-publication-replacement-probe" in gate
    assert "Move-Item -LiteralPath $replacement -Destination $probe" in gate
    assert "Publication digest verifier accepted a replaced package probe" in gate
    assert "Assert-PackageDigest -Path $package -Expected $expected" in gate


def test_post_upload_gate_hashes_the_exact_downloaded_artifact_against_earlier_digest() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    post_upload_start = workflow.index("- name: Verify GitHub-stored release ZIP identity")
    post_upload_gate = workflow[post_upload_start:]

    assert "Get-ChildItem -LiteralPath $proofRoot -Recurse -Filter 'Autosport-V1-windows-x64.zip' -File" in post_upload_gate
    assert "$env:AUTOSPORT_QUALIFIED_PACKAGE_SHA256" in post_upload_gate
    assert "GitHub-stored release ZIP SHA-256 mismatch" in post_upload_gate
