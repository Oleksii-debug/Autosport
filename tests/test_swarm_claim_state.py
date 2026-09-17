from __future__ import annotations

from datetime import datetime, timezone
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


def claim(run_id: str, lease_until: str, *, syntax: str = "=") -> str:
    return "\n".join(
        (
            "CLAIM_V1",
            f"RUN_ID{syntax}{run_id}",
            f"CLAIMED_AT{syntax}2026-09-17T17:00:00Z",
            f"LEASE_UNTIL{syntax}{lease_until}",
        )
    )


def test_future_renew_is_terminated_by_later_release() -> None:
    result = resolve_comments(
        [
            comment(100, claim("owner-a", "2026-09-17T17:20:00Z")),
            comment(
                110,
                "\n".join(
                    (
                        "CLAIM_RENEW_V1",
                        "RUN_ID=owner-a",
                        "LEASE_UNTIL=2026-09-17T18:00:00Z",
                    )
                ),
            ),
            comment(
                120,
                "\n".join(
                    (
                        "TASK_RESULT_V1",
                        "RUN_ID=owner-a",
                        "RESULT=PARTIAL",
                        "CLAIM_RELEASE_V1",
                        "RUN_ID=owner-a",
                        "REASON=handoff",
                    )
                ),
            ),
        ],
        now=NOW,
        capacity=1,
    )

    assert result["live_runs"] == []
    assert [run["run_id"] for run in result["released_runs"]] == ["owner-a"]
    assert result["released_runs"][0]["lease_until"] == "2026-09-17T18:00:00Z"


def test_multiple_ownership_events_in_one_comment_are_folded_in_line_order() -> None:
    result = resolve_comments(
        [
            comment(
                200,
                "\n".join(
                    (
                        "CLAIM_V1",
                        "RUN_ID: owner-a",
                        "CLAIMED_AT: 2026-09-17T17:00:00Z",
                        "LEASE_UNTIL: 2026-09-17T18:00:00Z",
                        "TASK_RESULT_V1",
                        "RUN_ID=owner-a",
                        "RESULT=COMPLETE",
                        "CLAIM_RELEASE_V1",
                        "RUN_ID: owner-a",
                        "REASON: completed",
                    )
                ),
            )
        ],
        now=NOW,
    )

    assert result["live_runs"] == []
    assert result["released_runs"][0]["latest_event"] == "release"
    assert result["malformed_events"] == []


def test_expired_lease_is_not_live() -> None:
    result = resolve_comments(
        [comment(300, claim("owner-a", "2026-09-17T17:39:59Z"))],
        now=NOW,
        capacity=1,
    )

    assert result["admitted_runs"] == []
    assert [run["run_id"] for run in result["expired_runs"]] == ["owner-a"]


def test_earlier_live_claim_survives_unrelated_recent_comments() -> None:
    result = resolve_comments(
        [
            comment(400, claim("owner-a", "2026-09-17T18:00:00Z")),
            comment(410, "AUDITOR_RECONCILIATION_V1\nSTATE=WAIT"),
            comment(420, "TASK_RESULT_V1\nRUN_ID=other\nRESULT=PARTIAL"),
        ],
        now=NOW,
        capacity=1,
    )

    assert [run["run_id"] for run in result["live_runs"]] == ["owner-a"]
    assert [run["run_id"] for run in result["admitted_runs"]] == ["owner-a"]


def test_renew_without_prior_claim_fails_closed() -> None:
    result = resolve_comments(
        [
            comment(
                500,
                "\n".join(
                    (
                        "CLAIM_RENEW_V1",
                        "RUN_ID=owner-a",
                        "LEASE_UNTIL=2026-09-17T18:00:00Z",
                    )
                ),
            )
        ],
        now=NOW,
    )

    assert result["live_runs"] == []
    assert result["malformed_events"][0]["kind"] == "renew_without_claim"


def test_duplicate_claim_is_ambiguous_and_never_admitted() -> None:
    result = resolve_comments(
        [
            comment(600, claim("owner-a", "2026-09-17T18:00:00Z")),
            comment(610, claim("owner-a", "2026-09-17T18:10:00Z")),
        ],
        now=NOW,
        capacity=1,
    )

    assert result["admitted_runs"] == []
    assert [run["run_id"] for run in result["ambiguous_runs"]] == ["owner-a"]
    assert result["malformed_events"][0]["kind"] == "duplicate_claim"


def test_capacity_uses_original_claim_order_after_dead_runs_are_removed() -> None:
    result = resolve_comments(
        [
            comment(700, claim("owner-a", "2026-09-17T18:00:00Z")),
            comment(710, claim("owner-b", "2026-09-17T18:00:00Z")),
            comment(720, claim("owner-c", "2026-09-17T18:00:00Z")),
            comment(
                730,
                "CLAIM_RELEASE_V1\nRUN_ID=owner-b\nREASON=handoff",
            ),
        ],
        now=NOW,
        capacity=1,
    )

    assert [run["run_id"] for run in result["live_runs"]] == [
        "owner-a",
        "owner-c",
    ]
    assert [run["run_id"] for run in result["admitted_runs"]] == ["owner-a"]
    assert [run["run_id"] for run in result["collision_candidates"]] == [
        "owner-c"
    ]


def test_missing_lease_is_reported_and_fails_closed() -> None:
    result = resolve_comments(
        [comment(800, "CLAIM_V1\nRUN_ID=owner-a\nCLAIMED_AT=2026-09-17T17:00:00Z")],
        now=NOW,
        capacity=1,
    )

    assert result["admitted_runs"] == []
    assert [run["run_id"] for run in result["ambiguous_runs"]] == ["owner-a"]
    assert result["malformed_events"][0]["kind"] == "missing_lease"


def test_decreasing_comment_ids_are_reported_without_reordering_history() -> None:
    result = resolve_comments(
        [
            comment(910, claim("owner-a", "2026-09-17T18:00:00Z")),
            comment(900, "CLAIM_RELEASE_V1\nRUN_ID=owner-a\nREASON=completed"),
        ],
        now=datetime(2026, 9, 17, 17, 40, tzinfo=timezone.utc),
    )

    assert result["live_runs"] == []
    assert result["released_runs"][0]["run_id"] == "owner-a"
    assert result["malformed_events"][0]["kind"] == "comment_order"
