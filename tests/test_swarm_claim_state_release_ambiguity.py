from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "swarm_claim_state.py"
SPEC = importlib.util.spec_from_file_location(
    "swarm_claim_state_release_ambiguity_test",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

resolve_comments = MODULE.resolve_comments


def test_release_terminates_but_does_not_clear_prior_ambiguity() -> None:
    result = resolve_comments(
        [
            {
                "id": 100,
                "body": "\n".join(
                    (
                        "CLAIM_V1",
                        "TASK_ID=task-a",
                        "SEMANTIC_KEY=control.test-v1",
                        "RUN_ID=owner-a",
                        "ACCOUNT_ID=A",
                        "CLAIM_MODE=SOURCE_MUTATION",
                        "CLAIMED_AT=2026-09-17T17:00:00Z",
                        "LEASE_UNTIL=2026-09-17T18:00:00Z",
                        "INTENDED_SLICE=isolated test slice",
                    )
                ),
            },
            {
                "id": 110,
                "body": "\n".join(
                    (
                        "CLAIM_HEARTBEAT_V1",
                        "RUN_ID=owner-a",
                        "ACCOUNT_ID=B",
                        "LEASE_UNTIL=2026-09-17T18:20:00Z",
                    )
                ),
            },
            {
                "id": 120,
                "body": "\n".join(
                    (
                        "CLAIM_RELEASE_V1",
                        "RUN_ID=owner-a",
                        "REASON=handoff",
                    )
                ),
            },
        ],
        now="2026-09-17T17:40:00Z",
        capacity=1,
    )

    assert result["live_runs"] == []
    assert result["admitted_runs"] == []
    assert result["released_runs"] == []
    assert [run["run_id"] for run in result["ambiguous_runs"]] == ["owner-a"]
    state = result["ambiguous_runs"][0]
    assert state["latest_event"] == "release"
    assert state["release_reason"] == "handoff"
    assert state["ambiguous"] is True
    assert state["account_id"] == "A"
    assert state["lease_until"] == "2026-09-17T18:00:00Z"
    assert any(
        event["kind"] == "renew_identity_conflict"
        for event in result["malformed_events"]
    )
