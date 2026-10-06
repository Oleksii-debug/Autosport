from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "swarm_branch_lease_guard.py"
SPEC = importlib.util.spec_from_file_location(
    "swarm_branch_lease_guard_canonical_resolver_test",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


SEMANTIC = "control.test-v1"
NOW = "2026-09-17T19:00:00Z"
OWNER = "owner-a"
COLLIDER = "owner-b"


def _run(run_id: str, *, lease_until: str = "2026-09-17T19:30:00Z") -> dict:
    return {
        "run_id": run_id,
        "semantic_key": SEMANTIC,
        "claim_mode": "SOURCE_MUTATION",
        "lease_until": lease_until,
    }


def _mutation(run_id: str) -> dict:
    return {
        "semantic_key": SEMANTIC,
        "branch": "canonical/source-branch",
        "prior_head": "old",
        "current_head": "new",
        "mutations": [
            {
                "head": "new",
                "run_id": run_id,
                "parent_heads": ["old"],
            }
        ],
    }


def test_canonical_admitted_runs_is_authoritative_over_live_runs() -> None:
    claims = {
        "admitted_runs": [_run(OWNER)],
        "live_runs": [_run(OWNER), _run(COLLIDER)],
        "collision_candidates": [_run(COLLIDER)],
        "ambiguous_runs": [],
        "malformed_events": [],
    }

    result = guard.evaluate_guard(claims, _mutation(OWNER), now=NOW)

    assert result["status"] == "OK"
    assert result["admitted_owner_run_id"] == OWNER


def test_collision_candidate_writer_is_rejected_against_canonical_admission() -> None:
    claims = {
        "admitted_runs": [_run(OWNER)],
        "live_runs": [_run(OWNER), _run(COLLIDER)],
        "collision_candidates": [_run(COLLIDER)],
        "ambiguous_runs": [],
        "malformed_events": [],
    }

    result = guard.evaluate_guard(claims, _mutation(COLLIDER), now=NOW)

    assert result["status"] == "COLLISION"
    assert result["admitted_owner_run_id"] == OWNER
    assert result["mutation_writer_run_ids"] == [COLLIDER]


def test_present_empty_admitted_runs_does_not_fall_back_to_live_runs() -> None:
    claims = {
        "admitted_runs": [],
        "live_runs": [_run(OWNER)],
        "collision_candidates": [_run(OWNER)],
        "ambiguous_runs": [],
        "malformed_events": [],
    }

    result = guard.evaluate_guard(claims, _mutation(OWNER), now=NOW)

    assert result["status"] == "NO_LIVE_OWNER"
    assert result["admitted_owner_run_id"] is None


def test_canonical_ambiguous_runs_fails_closed() -> None:
    claims = {
        "admitted_runs": [_run(OWNER)],
        "live_runs": [_run(OWNER)],
        "collision_candidates": [],
        "ambiguous_runs": [{"run_id": "ambiguous-owner"}],
        "malformed_events": [],
    }

    result = guard.evaluate_guard(claims, _mutation(OWNER), now=NOW)

    assert result["status"] == "AMBIGUOUS"
    assert "ambiguity" in result["evidence"][0]


def test_malformed_canonical_ambiguous_runs_shape_fails_closed() -> None:
    claims = {
        "admitted_runs": [_run(OWNER)],
        "live_runs": [_run(OWNER)],
        "collision_candidates": [],
        "ambiguous_runs": {"run_id": "corrupt-shape"},
        "malformed_events": [],
    }

    result = guard.evaluate_guard(claims, _mutation(OWNER), now=NOW)

    assert result["status"] == "AMBIGUOUS"
    assert result["admitted_owner_run_id"] is None
    assert "malformed evidence" in result["evidence"][0]


def test_malformed_events_non_list_shape_fails_closed() -> None:
    claims = {
        "admitted_runs": [_run(OWNER)],
        "live_runs": [_run(OWNER)],
        "collision_candidates": [],
        "ambiguous_runs": [],
        "malformed_events": "corrupt-shape",
    }

    result = guard.evaluate_guard(claims, _mutation(OWNER), now=NOW)

    assert result["status"] == "AMBIGUOUS"
    assert result["admitted_owner_run_id"] is None
    assert "malformed evidence" in result["evidence"][0]
