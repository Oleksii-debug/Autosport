from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "swarm_claim_state.py"
SPEC = importlib.util.spec_from_file_location(
    "swarm_claim_state_claim_mode_validation", SCRIPT_PATH
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

resolve_comments = MODULE.resolve_comments
NOW = "2026-09-17T19:10:00Z"


def _claim(comment_id: int, run_id: str, mode: str) -> dict[str, object]:
    return {
        "id": comment_id,
        "body": "\n".join(
            (
                "CLAIM_V1",
                "TASK_ID=task-a",
                "SEMANTIC_KEY=control.test-v1",
                f"RUN_ID={run_id}",
                "ACCOUNT_ID=A",
                f"CLAIM_MODE={mode}",
                "CLAIMED_AT=2026-09-17T19:00:00Z",
                "LEASE_UNTIL=2026-09-17T20:00:00Z",
                "INTENDED_SLICE=isolated test slice",
            )
        ),
    }


def test_unknown_claim_mode_fails_closed() -> None:
    result = resolve_comments(
        [_claim(100, "invalid-mode", "SOURCE_MUTATION_PLUS")],
        now=NOW,
        capacity=1,
    )

    assert result["live_runs"] == []
    assert result["source_live_runs"] == []
    assert result["non_source_live_runs"] == []
    assert result["admitted_runs"] == []
    assert result["collision_candidates"] == []
    assert result["released_runs"] == []
    assert result["expired_runs"] == []
    assert [run["run_id"] for run in result["ambiguous_runs"]] == ["invalid-mode"]
    assert any(
        event["kind"] == "invalid_claim_mode"
        and event["run_id"] == "invalid-mode"
        for event in result["malformed_events"]
    )


def test_every_protocol_claim_mode_remains_live_but_only_source_consumes_source_capacity() -> None:
    modes = (
        "SOURCE_MUTATION",
        "INTEGRATION",
        "READ_ONLY_AUDIT",
        "RESEARCH",
        "CI_TRIAGE",
    )
    comments = [
        _claim(200 + index, f"run-{index}", mode)
        for index, mode in enumerate(modes)
    ]

    result = resolve_comments(comments, now=NOW, capacity=1)

    assert [run["claim_mode"] for run in result["live_runs"]] == list(modes)
    assert [run["run_id"] for run in result["source_live_runs"]] == ["run-0"]
    assert [run["run_id"] for run in result["admitted_runs"]] == ["run-0"]
    assert result["collision_candidates"] == []
    assert [run["claim_mode"] for run in result["non_source_live_runs"]] == list(
        modes[1:]
    )
    assert result["ambiguous_runs"] == []
    assert result["malformed_events"] == []
