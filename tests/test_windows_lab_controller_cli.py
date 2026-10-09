"""Executable trust-boundary and non-executing manual workflow tests."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "windows_lab_contract.py"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-lab-contract.yml"


def _run(*args, input_text=""):
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        input=input_text, text=True, capture_output=True, timeout=15,
        cwd=ROOT,
    )


def test_cli_issue_roundtrip_is_source_bound_nonexecuting():
    source = "a" * 40
    package = "b" * 64
    issue = _run("issue", "--source-sha", source, "--package-sha256", package)
    assert issue.returncode == 0 and not issue.stderr
    record = json.loads(issue.stdout)
    assert record["source_sha"] == source
    assert record["package_sha256"] == package
    assert record["execution_authority"] is False
    assert record["human_tested"] is False
    assert record["nvda_verified"] is False
    assert record["target_machine_acceptance"] is False
    verified = _run("verify-ticket", input_text=issue.stdout)
    assert verified.returncode == 0
    assert verified.stdout.strip() == "TICKET_CONTRACT_OK ticket_id=" + record["ticket_id"]


def test_cli_stdin_adversarial_redaction_and_bounded_input():
    for candidate in ('{"secret":"PRIVATE_CANARY"}', "X" * 20000):
        received = _run("verify-ticket", input_text=candidate)
        assert received.returncode == 2
        assert received.stdout == ""
        assert received.stderr.strip() == "LAB_CONTRACT_REJECTED"
        assert "PRIVATE_CANARY" not in received.stderr


def test_cli_never_launders_campaign_as_machine_or_human_pass():
    from autosport.windows_lab_contract import (
        WindowsLabCampaign, WindowsLabObservation, source_level_lab_ticket,
    )
    ticket = source_level_lab_ticket("a" * 40, "b" * 64)
    report = WindowsLabCampaign(ticket).admit(
        WindowsLabObservation("desktop_start_stop", "PASS", "c" * 64)
    )
    verified = _run("verify-campaign", input_text=report.to_json())
    assert verified.returncode == 0
    assert verified.stdout.startswith("CAMPAIGN_UNVERIFIED ticket_id=")
    assert "observations=1" in verified.stdout
    assert "NVDA_VERIFIED" not in verified.stdout
    assert "PRIVATE" not in verified.stdout


def test_workflow_runs_only_owner_dispatched_exact_main_without_agent_or_fork_code():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "pull_request:" not in text
    assert "pull_request_target:" not in text
    assert "if: github.repository == 'Oleksii-debug/Autosport' && github.ref == 'refs/heads/main'" in text
    assert "persist-credentials: false" in text
    assert "ref: ${{ github.sha }}" in text
    assert 'test "$AUTOSPORT_SOURCE_SHA" = "$GITHUB_SHA"' in text
    assert "permissions:\n  contents: read" in text
    assert "scripts/verify_source_checkout.py" in text
    assert "scripts/windows_lab_contract.py verify-ticket" in text
    assert "retention-days: 7" in text
    assert "self-hosted" not in text
    assert "run-vm" not in text
    assert "workflow_run:" not in text
