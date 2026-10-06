from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "swarm_claim_state.py"
SPEC = importlib.util.spec_from_file_location("swarm_claim_state_claim_mode", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

resolve_comments = MODULE.resolve_comments
NOW = "2026-09-17T19:10:00Z"


def _comment(comment_id: int, body: str) -> dict[str, object]:
    return {"id": comment_id, "body": body}


def _claim(run_id: str, mode: str, claimed_at: str) -> str:
    return "\n".join(
        (
            "CLAIM_V1",
            "TASK_ID=task-a",
            "SEMANTIC_KEY=control.test-v1",
            f"RUN_ID={run_id}",
            "ACCOUNT_ID=A",
            f"CLAIM_MODE={mode}",
            f"CLAIMED_AT={claimed_at}",
            "LEASE_UNTIL=2026-09-17T20:00:00Z",
            "INTENDED_SLICE=isolated test slice",
        )
    )


def test_read_only_claim_does_not_consume_source_capacity() -> None:
    result = resolve_comments(
        [
            _comment(100, _claim("audit-first", "READ_ONLY_AUDIT", "2026-09-17T19:00:00Z")),
            _comment(110, _claim("source-owner", "SOURCE_MUTATION", "2026-09-17T19:01:00Z")),
        ],
        now=NOW,
        capacity=1,
    )

    assert [run["run_id"] for run in result["live_runs"]] == [
        "audit-first",
        "source-owner",
    ]
    assert [run["run_id"] for run in result["non_source_live_runs"]] == [
        "audit-first"
    ]
    assert [run["run_id"] for run in result["source_live_runs"]] == [
        "source-owner"
    ]
    assert [run["run_id"] for run in result["admitted_runs"]] == [
        "source-owner"
    ]
    assert result["collision_candidates"] == []


def test_source_capacity_preserves_source_server_order_only() -> None:
    result = resolve_comments(
        [
            _comment(200, _claim("audit-first", "READ_ONLY_AUDIT", "2026-09-17T19:00:00Z")),
            _comment(210, _claim("source-a", "SOURCE_MUTATION", "2026-09-17T19:01:00Z")),
            _comment(220, _claim("audit-second", "READ_ONLY_AUDIT", "2026-09-17T19:02:00Z")),
            _comment(230, _claim("source-b", "SOURCE_MUTATION", "2026-09-17T19:03:00Z")),
        ],
        now=NOW,
        capacity=1,
    )

    assert [run["run_id"] for run in result["admitted_runs"]] == ["source-a"]
    assert [run["run_id"] for run in result["collision_candidates"]] == [
        "source-b"
    ]
    assert [run["run_id"] for run in result["non_source_live_runs"]] == [
        "audit-first",
        "audit-second",
    ]
