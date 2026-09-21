from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

import pytest

from autosport.opponent_intelligence import IdentityView
from autosport.sport_memory_runtime import (
    DecisionMemoryConsumption,
    SportMemoryArtifact,
    SportMemoryError,
    SportMemoryMatchupEvidence,
    SportMemoryRuntime,
    SportMemoryScope,
    _digest,
)


GENERATION = "d" * 64
T1 = "2026-09-20T10:00:00Z"
T2 = "2026-09-20T10:05:00Z"
T2_CONSUMED = "2026-09-20T10:05:01Z"
T2_RETRY = "2026-09-20T10:05:09Z"


class _UnusedAuthority:
    def build_snapshots(self, **kwargs):
        raise AssertionError("snapshot authority is not used by this focused test")


class _LowLevelRuntime(SportMemoryRuntime):
    """Focused durable-store seam; never product-positive authority."""

    def _require_durable_positive_authority(self) -> None:
        return None

    def verify_matchup_evidence(self, matchup):
        if type(matchup) is not SportMemoryMatchupEvidence:
            raise TypeError("matchup must be SportMemoryMatchupEvidence")
        if _digest(matchup.payload(include_id=False)) != matchup.matchup_id:
            raise SportMemoryError("sport-memory matchup digest mismatch")
        return matchup


def _scope() -> SportMemoryScope:
    return SportMemoryScope(
        sport_id="tennis",
        league_entity_id="atp",
        market_context_id="match-winner",
    )


def _artifact(participant: str, marker: str) -> SportMemoryArtifact:
    performance_id = sha256(f"{participant}|performance".encode()).hexdigest()
    input_digest = _digest([performance_id])
    candidate = SportMemoryArtifact(
        memory_id="0" * 64,
        participant_entity_id=participant,
        scope=_scope(),
        identity_view=IdentityView.AS_KNOWN_AT_DECISION,
        causal_cutoff=T1,
        published_at=T1,
        rating_snapshot_id=sha256(f"{marker}|rating".encode()).hexdigest(),
        feature_snapshot_id=sha256(f"{marker}|feature".encode()).hexdigest(),
        input_digest=input_digest,
        input_performance_ids=(performance_id,),
        support=1,
        effective_sample=1,
        opponent_count=1,
        rating="0.5",
        uncertainty="0.2",
        state="SUPPORTED",
        last_observed_at=T1,
        age_seconds=0,
        authority_generation_sha256=GENERATION,
    )
    return replace(
        candidate,
        memory_id=_digest(candidate.payload(include_id=False)),
    )


def _runtime(tmp_path) -> tuple[_LowLevelRuntime, SportMemoryArtifact, SportMemoryArtifact]:
    runtime = _LowLevelRuntime.initialize_pristine(
        tmp_path / "sport-memory.json",
        _UnusedAuthority(),
        authority_generation_sha256=GENERATION,
    )
    subject = _artifact("participant-a", "a")
    opponent = _artifact("participant-b", "b")
    runtime._artifacts[subject.memory_id] = subject
    runtime._artifacts[opponent.memory_id] = opponent
    runtime._persist()
    return runtime, subject, opponent


def _seed_legacy_consumption(
    runtime: _LowLevelRuntime,
    *,
    decision_id: str,
    memory_id: str,
    decision_cutoff: str,
    consumed_at: str,
    expected_scope: SportMemoryScope,
    expected_view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
) -> DecisionMemoryConsumption:
    """Seed one exact pre-atomicity durable record without using public authority.

    These tests reproduce bytes that an older product version could already have
    persisted before the current bound-runtime guard existed. Constructing that
    historical prefix through today's guarded public method would test the guard,
    not restart compatibility.
    """
    artifact = runtime.get(memory_id)
    assert artifact.scope == expected_scope
    assert artifact.identity_view is expected_view
    payload = {
        "decision_id": decision_id,
        "memory_id": artifact.memory_id,
        "decision_cutoff": decision_cutoff,
        "consumed_at": consumed_at,
    }
    record = DecisionMemoryConsumption(
        consumption_id=_digest(payload),
        **payload,
    )
    runtime._validate_consumption(record, artifact)
    runtime._assert_no_participant_rebind(
        decision_id=decision_id,
        artifact=artifact,
    )
    key = (decision_id, artifact.memory_id)
    assert key not in runtime._decision_consumptions
    assert record.consumption_id not in runtime._consumptions
    runtime._consumptions[record.consumption_id] = record
    runtime._decision_consumptions[key] = record.consumption_id
    runtime._persist()
    return record


def _matchup(
    subject: SportMemoryArtifact,
    opponent: SportMemoryArtifact,
) -> SportMemoryMatchupEvidence:
    candidate = SportMemoryMatchupEvidence(
        matchup_id="0" * 64,
        subject_memory_id=subject.memory_id,
        opponent_memory_id=opponent.memory_id,
        subject_participant_entity_id=subject.participant_entity_id,
        opponent_participant_entity_id=opponent.participant_entity_id,
        scope=subject.scope,
        identity_view=subject.identity_view,
        as_of=T2,
        causal_cutoff=T1,
        published_at=T1,
        subject_support=subject.support,
        opponent_support=opponent.support,
        subject_uncertainty=subject.uncertainty,
        opponent_uncertainty=opponent.uncertainty,
        subject_age_seconds=300,
        opponent_age_seconds=300,
        authority_generation_sha256=GENERATION,
    )
    return replace(
        candidate,
        matchup_id=_digest(candidate.payload(include_id=False)),
    )


def test_matchup_pair_has_no_single_member_persist_boundary(tmp_path):
    runtime, subject, opponent = _runtime(tmp_path)
    matchup = _matchup(subject, opponent)
    original_persist = runtime._persist
    persist_snapshots: list[tuple[str, ...]] = []

    def fail_before_atomic_replace() -> None:
        persist_snapshots.append(
            tuple(
                sorted(
                    record.memory_id
                    for record in runtime._consumptions.values()
                    if record.decision_id == "decision-atomic"
                )
            )
        )
        raise RuntimeError("simulated crash before checkpoint replace")

    runtime._persist = fail_before_atomic_replace
    with pytest.raises(RuntimeError, match="simulated crash"):
        runtime.record_matchup_consumption(
            decision_id="decision-atomic",
            matchup=matchup,
            decision_cutoff=T2,
            consumed_at=T2_CONSUMED,
        )

    assert persist_snapshots == [
        tuple(sorted((subject.memory_id, opponent.memory_id)))
    ]
    assert runtime.consumptions_for_decision("decision-atomic") == ()

    reopened = _LowLevelRuntime(
        runtime.path,
        _UnusedAuthority(),
        authority_generation_sha256=GENERATION,
    )
    assert reopened.consumptions_for_decision("decision-atomic") == ()

    runtime._persist = original_persist
    records = runtime.record_matchup_consumption(
        decision_id="decision-atomic",
        matchup=matchup,
        decision_cutoff=T2,
        consumed_at=T2_RETRY,
    )
    assert {record.memory_id for record in records} == {
        subject.memory_id,
        opponent.memory_id,
    }
    assert {record.consumed_at for record in records} == {T2_RETRY}

    restarted = _LowLevelRuntime(
        runtime.path,
        _UnusedAuthority(),
        authority_generation_sha256=GENERATION,
    )
    assert restarted.consumptions_for_decision("decision-atomic") == records


def test_legacy_single_member_crash_prefix_recovers_with_original_timestamp(tmp_path):
    runtime, subject, opponent = _runtime(tmp_path)
    matchup = _matchup(subject, opponent)

    legacy_subject = _seed_legacy_consumption(
        runtime,
        decision_id="decision-recover",
        memory_id=subject.memory_id,
        decision_cutoff=T2,
        consumed_at=T2_CONSUMED,
        expected_scope=matchup.scope,
        expected_view=matchup.identity_view,
    )
    assert runtime.consumptions_for_decision("decision-recover") == (legacy_subject,)

    restarted = _LowLevelRuntime(
        runtime.path,
        _UnusedAuthority(),
        authority_generation_sha256=GENERATION,
    )
    records = restarted.record_matchup_consumption(
        decision_id="decision-recover",
        matchup=matchup,
        decision_cutoff=T2,
        consumed_at=T2_RETRY,
    )

    assert {record.memory_id for record in records} == {
        subject.memory_id,
        opponent.memory_id,
    }
    assert {record.consumed_at for record in records} == {T2_CONSUMED}
    assert records[0] == legacy_subject

    reread = _LowLevelRuntime(
        runtime.path,
        _UnusedAuthority(),
        authority_generation_sha256=GENERATION,
    )
    assert reread.consumptions_for_decision("decision-recover") == records


def test_legacy_partial_with_different_cutoff_still_fails_closed(tmp_path):
    runtime, subject, opponent = _runtime(tmp_path)
    matchup = _matchup(subject, opponent)

    _seed_legacy_consumption(
        runtime,
        decision_id="decision-conflict",
        memory_id=subject.memory_id,
        decision_cutoff=T1,
        consumed_at=T1,
        expected_scope=matchup.scope,
        expected_view=matchup.identity_view,
    )

    restarted = _LowLevelRuntime(
        runtime.path,
        _UnusedAuthority(),
        authority_generation_sha256=GENERATION,
    )
    with pytest.raises(SportMemoryError, match="decision consumption semantic drift"):
        restarted.record_matchup_consumption(
            decision_id="decision-conflict",
            matchup=matchup,
            decision_cutoff=T2,
            consumed_at=T2_RETRY,
        )

    remaining = restarted.consumptions_for_decision("decision-conflict")
    assert len(remaining) == 1
    assert remaining[0].memory_id == subject.memory_id
