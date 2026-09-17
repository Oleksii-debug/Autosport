from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "swarm_claim_state.py"
SPEC = importlib.util.spec_from_file_location("swarm_claim_state", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

resolve_comments = MODULE.resolve_comments

NOW = "2026-09-17T17:40:00Z"


def comment(comment_id: int, body: str) -> dict[str, object]:
    return {"id": comment_id, "body": body}


def claim(run_id: str, lease_until: str = "2026-09-17T18:00:00Z") -> str:
    return "\n".join(
        (
            "CLAIM_V1",
            "TASK_ID=task-a",
            "SEMANTIC_KEY=control.test-v1",
            f"RUN_ID={run_id}",
            "ACCOUNT_ID=A",
            "CLAIM_MODE=SOURCE_MUTATION",
            "CLAIMED_AT=2026-09-17T17:00:00Z",
            f"LEASE_UNTIL={lease_until}",
            "INTENDED_SLICE=isolated test slice",
        )
    )


def duplicate_field_event(result: dict[str, object]) -> dict[str, object]:
    events = result["malformed_events"]
    assert isinstance(events, list)
    matches = [event for event in events if event["kind"] == "duplicate_field"]
    assert len(matches) == 1
    return matches[0]


def test_conflicting_duplicate_run_id_claim_fails_closed() -> None:
    result = resolve_comments(
        [comment(1900, claim("owner-a") + "\nRUN_ID=owner-b")],
        now=NOW,
        capacity=1,
    )

    assert result["live_runs"] == []
    assert result["admitted_runs"] == []
    assert result["ambiguous_runs"] == []
    event = duplicate_field_event(result)
    assert "RUN_ID" in event["message"]
    assert "run_id" not in event


def test_identical_duplicate_run_id_claim_still_fails_closed() -> None:
    result = resolve_comments(
        [comment(1910, claim("owner-a") + "\nRUN_ID=owner-a")],
        now=NOW,
        capacity=1,
    )

    assert result["live_runs"] == []
    assert result["admitted_runs"] == []
    event = duplicate_field_event(result)
    assert event["run_id"] == "owner-a"
    assert "RUN_ID" in event["message"]


def test_duplicate_lease_until_claim_fails_closed() -> None:
    result = resolve_comments(
        [
            comment(
                1920,
                claim("owner-a") + "\nLEASE_UNTIL=2026-09-17T19:00:00Z",
            )
        ],
        now=NOW,
        capacity=1,
    )

    assert result["live_runs"] == []
    assert result["admitted_runs"] == []
    event = duplicate_field_event(result)
    assert event["run_id"] == "owner-a"
    assert "LEASE_UNTIL" in event["message"]


def test_duplicate_immutable_identity_claim_fails_closed() -> None:
    result = resolve_comments(
        [comment(1930, claim("owner-a") + "\nACCOUNT_ID=B")],
        now=NOW,
        capacity=1,
    )

    assert result["live_runs"] == []
    assert result["admitted_runs"] == []
    event = duplicate_field_event(result)
    assert event["run_id"] == "owner-a"
    assert "ACCOUNT_ID" in event["message"]


def test_duplicate_renewal_field_taints_existing_live_claim() -> None:
    result = resolve_comments(
        [
            comment(1940, claim("owner-a", "2026-09-17T18:10:00Z")),
            comment(
                1950,
                "\n".join(
                    (
                        "CLAIM_HEARTBEAT_V1",
                        "RUN_ID=owner-a",
                        "LEASE_UNTIL=2026-09-17T18:20:00Z",
                        "LEASE_UNTIL=2026-09-17T18:30:00Z",
                        "STATE=continuing",
                    )
                ),
            ),
        ],
        now=NOW,
        capacity=1,
    )

    assert result["live_runs"] == []
    assert result["admitted_runs"] == []
    assert [run["run_id"] for run in result["ambiguous_runs"]] == ["owner-a"]
    event = duplicate_field_event(result)
    assert event["run_id"] == "owner-a"
    assert "LEASE_UNTIL" in event["message"]
