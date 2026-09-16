import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "windows-build.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_release_workflow_uses_only_immutable_external_action_commits() -> None:
    text = _workflow_text()
    uses = re.findall(r"(?m)^\s*-\s+uses:\s+([^\s#]+)", text)
    assert uses, "release workflow must contain external action uses"

    for action in uses:
        assert re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}", action
        ), f"release workflow action is not pinned to a full commit SHA: {action}"


def test_release_workflow_declares_read_only_contents_permission() -> None:
    text = _workflow_text()
    assert re.search(r"(?m)^permissions:\s*$", text)
    assert re.search(r"(?m)^\s{2}contents:\s+read\s*$", text)


def test_release_workflow_checkout_does_not_persist_credentials() -> None:
    text = _workflow_text()
    checkout = re.search(
        r"(?ms)^\s*-\s+uses:\s+actions/checkout@[0-9a-f]{40}[^\n]*\n"
        r"\s+with:\s*\n"
        r"(?P<body>(?:\s{10}[^\n]+\n)+)",
        text,
    )
    assert checkout is not None, "pinned checkout step with with: block is required"
    body = checkout.group("body")
    assert re.search(r"(?m)^\s+persist-credentials:\s+false\s*$", body)
    assert re.search(r"(?m)^\s+fetch-depth:\s+0\s*$", body)
    assert "github.event.pull_request.head.sha || github.sha" in body


def test_release_workflow_keeps_packaged_evidence_smoke_closure() -> None:
    text = _workflow_text()
    assert 'AUTOSPORT_QUALIFIED_PACKAGE_SHA256=$packageSha' in text
    assert 'Execute packaged evidence export/verify contract' in text
    assert './scripts/evidence_export_package_smoke.ps1' in text
    assert 'Autosport-packaged-evidence-export-smoke' in text
