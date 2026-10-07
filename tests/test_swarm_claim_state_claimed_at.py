from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "swarm_claim_state.py"
SPEC = importlib.util.spec_from_file_location(
    "swarm_claim_state_claimed_at_test",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

resolve_comments = MODULE.resolve_comments


def test_renew_cannot_rewrite_claimed_at() -> None:
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
                        "LEASE_UNTIL=2026-09-17T17:50:00Z",
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
                        "CLAIMED_AT=2026-09-17T17:01:00Z",
                        "LEASE_UNTIL=2026-09-17T18:20:00Z",
                    )
                ),
            },
        ],
        now="2026-09-17T17:40:00Z",
        capacity=1,
    )

    assert result["live_runs"] == []
    assert result["admitted_runs"] == []
    assert [run["run_id"] for run in result["ambiguous_runs"]] == ["owner-a"]
    assert result["ambiguous_runs"][0]["claimed_at"] == "2026-09-17T17:00:00Z"
    assert result["ambiguous_runs"][0]["lease_until"] == "2026-09-17T17:50:00Z"
    assert result["malformed_events"][0]["kind"] == "renew_identity_conflict"
    assert "CLAIMED_AT" in result["malformed_events"][0]["message"]
