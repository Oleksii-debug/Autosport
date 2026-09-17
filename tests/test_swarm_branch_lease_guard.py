from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "swarm_branch_lease_guard.py"
SPEC = importlib.util.spec_from_file_location("swarm_branch_lease_guard", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


SEMANTIC = "economics.economic-goal-risk-contract-end-to-end-v1"
NOW = "2026-09-17T18:46:30Z"
A = "SOL-20260917T183826Z-483-FINISHER"
B = "OTHER-20260917T184500Z-483"


def claim_state(
    run_id: str = A,
    *,
    lease_until: str = "2026-09-17T18:58:26Z",
    semantic_key: str = SEMANTIC,
) -> dict:
    return {
        "admitted": [
            {
                "run_id": run_id,
                "semantic_key": semantic_key,
                "claim_mode": "SOURCE_MUTATION",
                "lease_until": lease_until,
            }
        ],
        "ambiguous": [],
        "malformed_events": [],
        "collision_candidates": [],
    }


def mutation_state(
    *,
    prior: str = "ec35e21",
    current: str = "3c9a161",
    mutations: list[dict] | None = None,
) -> dict:
    return {
        "semantic_key": SEMANTIC,
        "branch": "swarm/a-483-economic-goal-risk-20260917",
        "prior_head": prior,
        "current_head": current,
        "mutations": [] if mutations is None else mutations,
    }


def test_foreign_writer_reproduces_483_484_dual_writer_collision() -> None:
    result = guard.evaluate_guard(
        claim_state(),
        mutation_state(
            mutations=[
                {"head": "526ad252", "run_id": B},
                {"head": "3c9a161", "run_id": B},
            ]
        ),
        now=NOW,
    )
    assert result["status"] == "COLLISION"
    assert result["admitted_owner_run_id"] == A
    assert result["mutation_writer_run_ids"] == [B, B]


def test_same_owner_branch_movement_is_ok() -> None:
    result = guard.evaluate_guard(
        claim_state(),
        mutation_state(
            mutations=[
                {"head": "526ad252", "run_id": A},
                {"head": "3c9a161", "run_id": A},
            ]
        ),
        now=NOW,
    )
    assert result["status"] == "OK"


def test_release_successor_shape_is_ok_when_resolver_admits_successor() -> None:
    result = guard.evaluate_guard(
        claim_state(run_id=B, lease_until="2026-09-17T19:05:00Z"),
        mutation_state(mutations=[{"head": "3c9a161", "run_id": B}]),
        now=NOW,
    )
    assert result["status"] == "OK"
    assert result["admitted_owner_run_id"] == B


def test_missing_writer_identity_after_branch_movement_fails_closed() -> None:
    result = guard.evaluate_guard(
        claim_state(),
        mutation_state(mutations=[{"head": "3c9a161"}]),
        now=NOW,
    )
    assert result["status"] == "AMBIGUOUS"


def test_branch_movement_without_mutation_evidence_fails_closed() -> None:
    result = guard.evaluate_guard(
        claim_state(),
        mutation_state(mutations=[]),
        now=NOW,
    )
    assert result["status"] == "AMBIGUOUS"


def test_expired_owner_is_not_treated_as_live_authority() -> None:
    result = guard.evaluate_guard(
        claim_state(lease_until="2026-09-17T18:46:00Z"),
        mutation_state(mutations=[{"head": "3c9a161", "run_id": B}]),
        now=NOW,
    )
    assert result["status"] == "NO_LIVE_OWNER"
    assert result["admitted_owner_run_id"] is None


def test_no_branch_movement_is_ok_for_live_owner() -> None:
    result = guard.evaluate_guard(
        claim_state(),
        mutation_state(prior="same", current="same"),
        now=NOW,
    )
    assert result["status"] == "OK"
    assert result["branch_moved"] is False


def test_claim_state_ambiguity_fails_closed_before_writer_check() -> None:
    claims = claim_state()
    claims["ambiguous"] = [{"reason": "server-order anomaly"}]
    result = guard.evaluate_guard(
        claims,
        mutation_state(mutations=[{"head": "3c9a161", "run_id": A}]),
        now=NOW,
    )
    assert result["status"] == "AMBIGUOUS"


def test_multiple_live_source_owners_fail_closed() -> None:
    claims = claim_state()
    claims["admitted"].append(
        {
            "run_id": B,
            "semantic_key": SEMANTIC,
            "claim_mode": "SOURCE_MUTATION",
            "lease_until": "2026-09-17T19:05:00Z",
        }
    )
    result = guard.evaluate_guard(
        claims,
        mutation_state(mutations=[{"head": "3c9a161", "run_id": A}]),
        now=NOW,
    )
    assert result["status"] == "AMBIGUOUS"


def test_mutation_evidence_must_bind_to_current_head() -> None:
    result = guard.evaluate_guard(
        claim_state(),
        mutation_state(
            current="3c9a161",
            mutations=[{"head": "526ad252", "run_id": A}],
        ),
        now=NOW,
    )
    assert result["status"] == "AMBIGUOUS"


def test_duplicate_mutation_head_is_ambiguous() -> None:
    result = guard.evaluate_guard(
        claim_state(),
        mutation_state(
            mutations=[
                {"head": "3c9a161", "run_id": A},
                {"head": "3c9a161", "run_id": A},
            ]
        ),
        now=NOW,
    )
    assert result["status"] == "AMBIGUOUS"


def test_non_source_admitted_run_does_not_gain_source_authority() -> None:
    claims = claim_state()
    claims["admitted"][0]["claim_mode"] = "READ_ONLY_AUDIT"
    result = guard.evaluate_guard(
        claims,
        mutation_state(mutations=[{"head": "3c9a161", "run_id": A}]),
        now=NOW,
    )
    assert result["status"] == "NO_LIVE_OWNER"


def test_semantic_key_mismatch_is_rejected() -> None:
    try:
        guard.evaluate_guard(
            claim_state(),
            mutation_state(mutations=[{"head": "3c9a161", "run_id": A}]),
            now=NOW,
            semantic_key="other.semantic.key",
        )
    except ValueError as exc:
        assert "semantic_key" in str(exc)
    else:
        raise AssertionError("semantic key mismatch must fail")


def test_cli_returns_nonzero_for_collision(tmp_path, capsys) -> None:
    claims_path = tmp_path / "claims.json"
    mutation_path = tmp_path / "mutation.json"
    claims_path.write_text(json.dumps(claim_state()), encoding="utf-8")
    mutation_path.write_text(
        json.dumps(
            mutation_state(
                mutations=[{"head": "3c9a161", "run_id": B}]
            )
        ),
        encoding="utf-8",
    )

    exit_code = guard.main(
        [
            "--claim-state",
            str(claims_path),
            "--mutation-state",
            str(mutation_path),
            "--now",
            NOW,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "COLLISION"
