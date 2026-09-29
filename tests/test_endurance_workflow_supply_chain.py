from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "endurance.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_endurance_workflow_uses_only_immutable_external_action_commits() -> None:
    text = _workflow_text()
    uses = re.findall(r"(?m)^\s*-\s+uses:\s+([^\s#]+)", text)
    assert uses, "Endurance workflow must contain external action uses"

    for action in uses:
        assert re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}", action
        ), f"Endurance workflow action is not pinned to a full commit SHA: {action}"


def test_endurance_workflow_declares_read_only_contents_permission() -> None:
    text = _workflow_text()
    assert re.search(r"(?m)^permissions:\s*$", text)
    assert re.search(r"(?m)^\s{2}contents:\s+read\s*$", text)


def test_endurance_checkout_is_exact_head_and_does_not_persist_credentials() -> None:
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


def test_endurance_binds_execution_to_exact_event_head() -> None:
    text = _workflow_text()
    exact_head = r"${{ github.event.pull_request.head.sha || github.sha }}"
    assert f"AUTOSPORT_SOURCE_SHA: {exact_head}" in text
    assert f"ref: {exact_head}" in text
    assert 'python scripts/verify_source_checkout.py --source-sha "${{ env.AUTOSPORT_SOURCE_SHA }}"' in text


def test_endurance_retains_cross_platform_bounded_gate_and_evidence_upload() -> None:
    text = _workflow_text()
    assert "os: [ubuntu-latest, windows-latest]" in text
    assert "--events 20000" in text
    assert "--restart-cycles 3" in text
    assert "tests/test_collector_endurance_composition.py" in text
    assert "endurance-report.json" in text
