from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from autosport.opponent_graph import (
    IdentityView,
    MatchResult,
    OpponentGraphError,
    OpponentGraphStore,
    PerformanceOutcome,
    RecomputeStatus,
)
from autosport.participant_identity import EntityIdentity, EntityKind, ParticipantIdentityRegistry


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-02T00:00:00Z"
T2 = "2026-01-03T00:00:00Z"
T3 = "2026-01-04T00:00:00Z"


def make_registry(path: Path) -> ParticipantIdentityRegistry:
    registry = ParticipantIdentityRegistry.initialize_pristine(path)
    for entity_id, digest in (("p-a", SHA_A), ("p-b", SHA_B), ("p-c", SHA_C)):
        registry.add_entity(
            EntityIdentity(
                entity_id=entity_id,
                kind=EntityKind.PARTICIPANT,
                source_reference=f"provider:{entity_id}",
                evidence_sha256=digest,
                first_known_at=T0,
                available_at=T0,
            )
        )
    return registry


def outcome(
    event_id: str,
    a: str,
    b: str,
    *,
    result: MatchResult,
    observed_at: str,
    available_at: str,
    digest: str = SHA_A,
    supersedes: str | None = None,
) -> PerformanceOutcome:
    return PerformanceOutcome(
        event_id=event_id,
        sport_id="football",
        participant_a_id=a,
        participant_b_id=b,
        result=result,
        observed_at=observed_at,
        available_at=available_at,
        evidence_sha256=digest,
        league_id="league-1",
        market_id="market-1",
        supersedes_observation_id=supersedes,
    )


def make_store(tmp_path: Path) -> OpponentGraphStore:
    identity = make_registry(tmp_path / "identity.json")
    return OpponentGraphStore(tmp_path / "graph.json", identity)


def test_future_outcome_is_excluded_from_decision_time_and_late_backfill_is_research_only(tmp_path: Path):
    store = make_store(tmp_path)
    late = outcome(
        "event-future",
        "p-a",
        "p-b",
        result=MatchResult.A_WIN,
        observed_at=T2,
        available_at=T3,
    )
    store.add_observation(late)

    with pytest.raises(OpponentGraphError, match="insufficient causal evidence"):
        store.build_rating_snapshot(as_of=T1)

    restated = store.build_rating_snapshot(
        as_of=T2,
        view=IdentityView.RESTATED_RESEARCH,
    )
    assert restated.input_observation_ids == (late.observation_id,)

    with pytest.raises(OpponentGraphError, match="insufficient causal evidence"):
        store.build_rating_snapshot(as_of=T1, view=IdentityView.RESTATED_RESEARCH)


def test_duplicate_delivery_is_idempotent_and_restart_preserves_one_edge(tmp_path: Path):
    store = make_store(tmp_path)
    row = outcome(
        "event-1", "p-a", "p-b", result=MatchResult.A_WIN,
        observed_at=T1, available_at=T1,
    )
    first = store.add_observation(row)
    second = store.add_observation(row)
    assert first == second
    assert len(store.observations()) == 1
    assert len(store.edges()) == 1

    reopened = OpponentGraphStore(tmp_path / "graph.json", store.identity_registry)
    assert reopened.observations() == store.observations()
    assert reopened.edges() == store.edges()


def test_rating_snapshot_is_deterministic_under_reordered_input_delivery(tmp_path: Path):
    first = make_store(tmp_path / "first")
    rows = [
        outcome("event-1", "p-a", "p-b", result=MatchResult.A_WIN, observed_at=T1, available_at=T1, digest=SHA_A),
        outcome("event-2", "p-b", "p-c", result=MatchResult.DRAW, observed_at=T2, available_at=T2, digest=SHA_B),
    ]
    for row in rows:
        first.add_observation(row)
    snapshot_a = first.build_rating_snapshot(as_of=T3)
    first.publish_snapshot(snapshot_a)

    second = make_store(tmp_path / "second")
    for row in reversed(rows):
        second.add_observation(row)
    snapshot_b = second.build_rating_snapshot(as_of=T3)
    assert snapshot_a.snapshot_id == snapshot_b.snapshot_id
    assert snapshot_a.input_set_digest == snapshot_b.input_set_digest
    assert snapshot_a.features == snapshot_b.features


def test_unknown_canonical_identity_is_rejected_without_creating_graph_state(tmp_path: Path):
    store = make_store(tmp_path)
    row = outcome(
        "event-unknown", "p-a", "p-missing", result=MatchResult.A_WIN,
        observed_at=T1, available_at=T1,
    )
    with pytest.raises(OpponentGraphError, match="unknown canonical participant identity"):
        store.add_observation(row)
    assert store.observations() == ()
    assert store.edges() == ()


def test_published_snapshot_requires_all_inputs_to_be_durable(tmp_path: Path):
    store = make_store(tmp_path)
    row = outcome(
        "event-1", "p-a", "p-b", result=MatchResult.A_WIN,
        observed_at=T1, available_at=T1,
    )
    store.add_observation(row)
    snapshot = store.build_rating_snapshot(as_of=T2)

    forged = snapshot.__class__(
        algorithm=snapshot.algorithm,
        algorithm_version=snapshot.algorithm_version,
        config_digest=snapshot.config_digest,
        as_of=snapshot.as_of,
        view=snapshot.view,
        input_observation_ids=("f" * 64,),
        input_set_digest="f" * 64,
        features=snapshot.features,
        causal_cutoff=snapshot.causal_cutoff,
    )
    with pytest.raises(OpponentGraphError, match="observation not durably committed"):
        store.publish_snapshot(forged)


def test_outcome_correction_invalidates_edge_snapshot_and_emits_recompute_work(tmp_path: Path):
    store = make_store(tmp_path)
    original = outcome(
        "event-1", "p-a", "p-b", result=MatchResult.A_WIN,
        observed_at=T1, available_at=T1, digest=SHA_A,
    )
    store.add_observation(original)
    snapshot = store.build_rating_snapshot(as_of=T2)
    snapshot_id = store.publish_snapshot(snapshot)

    correction = outcome(
        "event-1-correction", "p-a", "p-b", result=MatchResult.B_WIN,
        observed_at=T1, available_at=T3, digest=SHA_C,
        supersedes=original.observation_id,
    )
    store.add_observation(correction)

    assert len(store.edges()) == 1
    assert store.edges()[0].observation_id == correction.observation_id
    invalidated = [item for item in store.snapshots() if item.snapshot_id == snapshot_id]
    assert len(invalidated) == 1
    assert invalidated[0].invalidated is True
    assert invalidated[0].snapshot_id == snapshot_id
    assert any(
        item.target_snapshot_id == snapshot_id and item.status is RecomputeStatus.REQUIRED
        for item in store.recompute_work()
    )
    assert len(store.edges(include_invalidated=True)) == 2


def test_identity_invalidation_preserves_snapshot_identity_and_marks_edge(tmp_path: Path):
    store = make_store(tmp_path)
    row = outcome(
        "event-1", "p-a", "p-b", result=MatchResult.A_WIN,
        observed_at=T1, available_at=T1,
    )
    store.add_observation(row)
    snapshot = store.build_rating_snapshot(as_of=T2)
    snapshot_id = store.publish_snapshot(snapshot)

    store.invalidate_for_identity("p-a", reason_code="ALIAS_CORRECTION")

    current = [item for item in store.snapshots() if item.snapshot_id == snapshot_id]
    assert len(current) == 1
    assert current[0].invalidated is True
    assert store.edges() == ()
    assert len(store.edges(include_invalidated=True)) == 1
    assert any(work.reason_code == "ALIAS_CORRECTION" for work in store.recompute_work())


def test_restated_snapshot_rejects_superseded_observation_when_correction_exists(tmp_path: Path):
    store = make_store(tmp_path)
    original = outcome(
        "event-1", "p-a", "p-b", result=MatchResult.A_WIN,
        observed_at=T1, available_at=T1,
    )
    store.add_observation(original)
    correction = outcome(
        "event-1-correction", "p-a", "p-b", result=MatchResult.B_WIN,
        observed_at=T1, available_at=T3, digest=SHA_C,
        supersedes=original.observation_id,
    )
    store.add_observation(correction)

    restated = store.build_rating_snapshot(as_of=T3, view=IdentityView.RESTATED_RESEARCH)
    assert restated.input_observation_ids == (correction.observation_id,)
    assert original.observation_id not in restated.input_observation_ids


def test_same_canonical_pairs_are_order_independent_for_edge_identity(tmp_path: Path):
    store = make_store(tmp_path)
    a = outcome(
        "event-1", "p-a", "p-b", result=MatchResult.A_WIN,
        observed_at=T1, available_at=T1,
    )
    b = outcome(
        "event-2", "p-b", "p-a", result=MatchResult.B_WIN,
        observed_at=T2, available_at=T2, digest=SHA_B,
    )
    store.add_observation(a)
    store.add_observation(b)
    assert len({edge.edge_id for edge in store.edges()}) == 2


def test_rating_features_have_positive_uncertainty_and_exact_support(tmp_path: Path):
    store = make_store(tmp_path)
    store.add_observation(
        outcome("event-1", "p-a", "p-b", result=MatchResult.A_WIN, observed_at=T1, available_at=T1)
    )
    store.add_observation(
        outcome("event-2", "p-a", "p-c", result=MatchResult.DRAW, observed_at=T2, available_at=T2, digest=SHA_B)
    )
    snapshot = store.build_rating_snapshot(as_of=T3)
    by_id = {feature.participant_id: feature for feature in snapshot.features}
    assert by_id["p-a"].support == 2
    assert by_id["p-b"].support == 1
    assert by_id["p-c"].support == 1
    assert all(feature.uncertainty > Decimal("0") for feature in snapshot.features)
