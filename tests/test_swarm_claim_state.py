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


def claim(
    run_id: str,
    lease_until: str,
    *,
    syntax: str = "=",
    omit: frozenset[str] = frozenset(),
) -> str:
    values = (
        ("TASK_ID", "task-a"),
        ("SEMANTIC_KEY", "control.test-v1"),
        ("RUN_ID", run_id),
        ("ACCOUNT_ID", "A"),
        ("CLAIM_MODE", "SOURCE_MUTATION"),
        ("CLAIMED_AT", "2026-09-17T17:00:00Z"),
        ("LEASE_UNTIL", lease_until),
        ("INTENDED_SLICE", "isolated test slice"),
    )
    return "\n".join(
        ["CLAIM_V1"]
        + [f"{key}{syntax}{value}" for key, value in values if key not in omit]
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
                        "TASK_ID: task-a",
                        "SEMANTIC_KEY: control.test-v1",
                        "RUN_ID: owner-a",
                        "ACCOUNT_ID: A",
                        "CLAIM_MODE: SOURCE_MUTATION",
                        "CLAIMED_AT: 2026-09-17T17:00:00Z",
                        "LEASE_UNTIL: 2026-09-17T18:00:00Z",
                        "INTENDED_SLICE: isolated test slice",
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
        [
            comment(
                800,
                claim(
                    "owner-a",
                    "2026-09-17T18:00:00Z",
                    omit=frozenset({"LEASE_UNTIL"}),
                ),
            )
        ],
        now=NOW,
        capacity=1,
    )

    assert result["admitted_runs"] == []
    assert [run["run_id"] for run in result["ambiguous_runs"]] == ["owner-a"]
    assert result["malformed_events"][0]["kind"] == "missing_lease"


def test_decreasing_comment_ids_fail_closed_for_all_ownership_state() -> None:
    result = resolve_comments(
        [
            comment(910, claim("owner-a", "2026-09-17T18:00:00Z")),
            comment(900, "CLAIM_RELEASE_V1\nRUN_ID=owner-a\nREASON=completed"),
        ],
        now=datetime(2026, 9, 17, 17, 40, tzinfo=timezone.utc),
        capacity=1,
    )

    assert result["live_runs"] == []
    assert result["admitted_runs"] == []
    assert result["released_runs"] == []
    assert [run["run_id"] for run in result["ambiguous_runs"]] == ["owner-a"]
    assert any(event["kind"] == "comment_order" for event in result["malformed_events"])


def test_reversed_release_then_claim_cannot_manufacture_live_owner() -> None:
    result = resolve_comments(
        [
            comment(920, "CLAIM_RELEASE_V1\nRUN_ID=owner-a\nREASON=completed"),
            comment(910, claim("owner-a", "2026-09-17T18:00:00Z")),
        ],
        now=NOW,
        capacity=1,
    )

    assert result["live_runs"] == []
    assert result["admitted_runs"] == []
    assert [run["run_id"] for run in result["ambiguous_runs"]] == ["owner-a"]
    assert {event["kind"] for event in result["malformed_events"]} >= {
        "release_without_claim",
        "comment_order",
    }


def test_claim_requires_complete_protocol_identity() -> None:
    for index, field in enumerate(
        (
            "TASK_ID",
            "SEMANTIC_KEY",
            "ACCOUNT_ID",
            "CLAIM_MODE",
            "INTENDED_SLICE",
        ),
        start=1000,
    ):
        run_id = f"owner-{field.lower()}"
        result = resolve_comments(
            [
                comment(
                    index,
                    claim(
                        run_id,
                        "2026-09-17T18:00:00Z",
                        omit=frozenset({field}),
                    ),
                )
            ],
            now=NOW,
            capacity=1,
        )

        assert result["live_runs"] == []
        assert result["admitted_runs"] == []
        assert [run["run_id"] for run in result["ambiguous_runs"]] == [run_id]
        assert result["malformed_events"][0]["kind"] == "missing_claim_fields"
        assert field in result["malformed_events"][0]["message"]


def test_claim_requires_claimed_at() -> None:
    result = resolve_comments(
        [
            comment(
                1100,
                claim(
                    "owner-a",
                    "2026-09-17T18:00:00Z",
                    omit=frozenset({"CLAIMED_AT"}),
                ),
            )
        ],
        now=NOW,
        capacity=1,
    )

    assert result["live_runs"] == []
    assert result["admitted_runs"] == []
    assert [run["run_id"] for run in result["ambiguous_runs"]] == ["owner-a"]
    assert result["malformed_events"][0]["kind"] == "missing_claimed_at"


def test_renew_cannot_rehabilitate_incomplete_claim_identity() -> None:
    result = resolve_comments(
        [
            comment(
                1200,
                claim(
                    "owner-a",
                    "2026-09-17T17:50:00Z",
                    omit=frozenset({"ACCOUNT_ID"}),
                ),
            ),
            comment(
                1210,
                "\n".join(
                    (
                        "CLAIM_RENEW_V1",
                        "RUN_ID=owner-a",
                        "ACCOUNT_ID=A",
                        "LEASE_UNTIL=2026-09-17T18:20:00Z",
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
    assert result["ambiguous_runs"][0]["account_id"] is None
    assert result["malformed_events"][0]["kind"] == "missing_claim_fields"


def test_renew_cannot_rewrite_immutable_claim_identity() -> None:
    result = resolve_comments(
        [
            comment(1300, claim("owner-a", "2026-09-17T17:50:00Z")),
            comment(
                1310,
                "\n".join(
                    (
                        "CLAIM_RENEW_V1",
                        "RUN_ID=owner-a",
                        "ACCOUNT_ID=B",
                        "LEASE_UNTIL=2026-09-17T18:20:00Z",
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
    assert result["ambiguous_runs"][0]["account_id"] == "A"
    assert result["malformed_events"][0]["kind"] == "renew_identity_conflict"


def test_claim_heartbeat_extends_lease_past_original_expiry() -> None:
    result = resolve_comments(
        [
            comment(1400, claim("owner-a", "2026-09-17T17:20:00Z")),
            comment(
                1410,
                "\n".join(
                    (
                        "CLAIM_HEARTBEAT_V1",
                        "RUN_ID=owner-a",
                        "LEASE_UNTIL=2026-09-17T18:20:00Z",
                        "STATE=continuing",
                    )
                ),
            ),
        ],
        now=NOW,
        capacity=1,
    )

    assert [run["run_id"] for run in result["live_runs"]] == ["owner-a"]
    assert [run["run_id"] for run in result["admitted_runs"]] == ["owner-a"]
    assert result["live_runs"][0]["lease_until"] == "2026-09-17T18:20:00Z"
    assert result["malformed_events"] == []


def test_release_after_claim_heartbeat_wins() -> None:
    result = resolve_comments(
        [
            comment(1500, claim("owner-a", "2026-09-17T17:20:00Z")),
            comment(
                1510,
                "CLAIM_HEARTBEAT_V1\nRUN_ID=owner-a\nLEASE_UNTIL=2026-09-17T18:20:00Z\nSTATE=continuing",
            ),
            comment(1520, "CLAIM_RELEASE_V1\nRUN_ID=owner-a\nREASON=handoff"),
        ],
        now=NOW,
        capacity=1,
    )

    assert result["live_runs"] == []
    assert result["admitted_runs"] == []
    assert [run["run_id"] for run in result["released_runs"]] == ["owner-a"]
    assert result["malformed_events"] == []


def test_claim_heartbeat_without_prior_claim_fails_closed() -> None:
    result = resolve_comments(
        [
            comment(
                1600,
                "CLAIM_HEARTBEAT_V1\nRUN_ID=owner-a\nLEASE_UNTIL=2026-09-17T18:20:00Z\nSTATE=continuing",
            )
        ],
        now=NOW,
        capacity=1,
    )

    assert result["live_runs"] == []
    assert result["admitted_runs"] == []
    assert result["malformed_events"][0]["kind"] == "renew_without_claim"
    assert result["malformed_events"][0]["header"] == "CLAIM_HEARTBEAT_V1"


def test_claim_heartbeat_cannot_rewrite_immutable_identity() -> None:
    result = resolve_comments(
        [
            comment(1700, claim("owner-a", "2026-09-17T17:50:00Z")),
            comment(
                1710,
                "\n".join(
                    (
                        "CLAIM_HEARTBEAT_V1",
                        "RUN_ID=owner-a",
                        "ACCOUNT_ID=B",
                        "LEASE_UNTIL=2026-09-17T18:20:00Z",
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
    assert result["ambiguous_runs"][0]["account_id"] == "A"
    assert result["malformed_events"][0]["kind"] == "renew_identity_conflict"
    assert result["malformed_events"][0]["header"] == "CLAIM_HEARTBEAT_V1"


def test_legacy_lease_renew_alias_extends_existing_claim_only() -> None:
    result = resolve_comments(
        [
            comment(1800, claim("owner-a", "2026-09-17T17:20:00Z")),
            comment(
                1810,
                "LEASE_RENEW_V1\nRUN_ID=owner-a\nLEASE_UNTIL=2026-09-17T18:20:00Z",
            ),
        ],
        now=NOW,
        capacity=1,
    )

    assert [run["run_id"] for run in result["admitted_runs"]] == ["owner-a"]
    assert result["admitted_runs"][0]["lease_until"] == "2026-09-17T18:20:00Z"
    assert result["malformed_events"] == []
