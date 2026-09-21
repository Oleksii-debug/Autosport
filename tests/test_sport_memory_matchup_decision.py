from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

import pytest

from autosport import _sport_memory_authority_guard as _guard
from autosport.opponent_intelligence import (
    FeatureSnapshot,
    IdentityView,
    RatingSnapshot,
    SnapshotState,
)
from autosport.portfolio_plan import OpportunityEvidence
from autosport.sport_memory_decision_evidence import (
    bind_sport_memory_to_opportunity_evidence,
)
from autosport.sport_memory_runtime import (
    SportMemoryError,
    SportMemoryRuntime as _PublicSportMemoryRuntime,
    SportMemoryScope,
)


GENERATION = "e" * 64
CODE_SHA = "c" * 64
DEPENDENCY_SHA = "d" * 64
T1 = "2026-09-20T10:00:00Z"
T1_PUBLISHED = "2026-09-20T10:00:01Z"
T2 = "2026-09-20T10:05:00Z"
T2_CONSUMED = "2026-09-20T10:05:01Z"
T3 = "2026-09-20T11:00:00Z"
T3_PUBLISHED = "2026-09-20T11:00:01Z"
T4 = "2026-09-20T11:05:00Z"
T4_CONSUMED = "2026-09-20T11:05:01Z"


def _digest(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(raw).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class _MatchupAuthority:
    def build_snapshots(self, **kwargs):
        participant = kwargs["participant_entity_id"]
        if participant not in {"participant-a", "participant-b"}:
            raise AssertionError("unexpected participant")
        cutoff = kwargs["causal_cutoff"]
        published = kwargs["published_at"]
        cutoff_dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        input_ids = (
            sha256(f"{participant}|{cutoff}|performance-1".encode()).hexdigest(),
            sha256(f"{participant}|{cutoff}|performance-2".encode()).hexdigest(),
        )
        input_digest = _digest(list(input_ids))
        rating_value = "0.70" if participant == "participant-a" else "0.40"
        if cutoff == T3:
            rating_value = (
                "0.75" if participant == "participant-a" else "0.35"
            )
        rating = RatingSnapshot(
            snapshot_id=sha256(
                f"{participant}|{cutoff}|rating".encode()
            ).hexdigest(),
            participant_entity_id=participant,
            sport_id=kwargs["sport_id"],
            league_id=kwargs["league_entity_id"],
            market_context_id=kwargs["market_context_id"],
            view=kwargs["view"],
            causal_cutoff=cutoff,
            published_at=published,
            algorithm_family="bounded-mean-score",
            algorithm_version=kwargs["algorithm_version"],
            config_sha256=sha256(b"matchup-config").hexdigest(),
            min_support=kwargs["min_support"],
            max_age_seconds=kwargs["max_age_seconds"],
            code_sha256=kwargs["code_sha256"],
            dependency_sha256=kwargs["dependency_sha256"],
            predecessor_snapshot_ids=(),
            input_performance_ids=input_ids,
            input_digest=input_digest,
            support=2,
            effective_sample=2,
            opponent_count=2,
            rating=rating_value,
            uncertainty="0.5",
            state=SnapshotState.SUPPORTED,
        )
        feature = FeatureSnapshot(
            snapshot_id=sha256(
                f"{participant}|{cutoff}|feature".encode()
            ).hexdigest(),
            rating_snapshot_id=rating.snapshot_id,
            participant_entity_id=participant,
            sport_id=rating.sport_id,
            league_id=rating.league_id,
            market_context_id=rating.market_context_id,
            view=rating.view,
            causal_cutoff=cutoff,
            published_at=published,
            input_digest=input_digest,
            support=2,
            effective_sample=2,
            opponent_count=2,
            last_observed_at=_iso(cutoff_dt - timedelta(hours=1)),
            age_seconds=3600,
            state=SnapshotState.SUPPORTED,
        )
        return rating, feature


class _LowLevelSportMemoryRuntime(_PublicSportMemoryRuntime):
    """Deterministic test seam; never a product-positive authority."""

    def _require_durable_positive_authority(self) -> None:
        return None

    def materialize(self, *args, **kwargs):
        return _guard._ORIGINAL_MATERIALIZE(self, *args, **kwargs)

    def record_consumption(self, *args, **kwargs):
        return _guard._ORIGINAL_RECORD_CONSUMPTION(self, *args, **kwargs)


SportMemoryRuntime = _LowLevelSportMemoryRuntime


def _scope() -> SportMemoryScope:
    return SportMemoryScope(
        sport_id="tennis",
        league_entity_id="atp",
        market_context_id="match-winner",
    )


def _runtime(tmp_path):
    return SportMemoryRuntime.initialize_pristine(
        tmp_path / "sport-memory.json",
        _MatchupAuthority(),
        authority_generation_sha256=GENERATION,
    )


def _materialize_pair(runtime, *, cutoff: str, published_at: str):
    artifacts = []
    for participant in ("participant-a", "participant-b"):
        artifacts.append(
            runtime.materialize(
                participant_entity_id=participant,
                scope=_scope(),
                causal_cutoff=cutoff,
                published_at=published_at,
                code_sha256=CODE_SHA,
                dependency_sha256=DEPENDENCY_SHA,
            )
        )
    return tuple(artifacts)


def test_matchup_evidence_binds_two_memories_into_normal_opportunity_evidence(
    tmp_path,
):
    runtime = _runtime(tmp_path)
    _materialize_pair(runtime, cutoff=T1, published_at=T1_PUBLISHED)

    matchup = runtime.matchup_as_of(
        "participant-a",
        "participant-b",
        _scope(),
        as_of=T2,
    )
    assert matchup.subject_support == 2
    assert matchup.opponent_support == 2
    assert matchup.subject_uncertainty == "0.5"
    assert matchup.opponent_uncertainty == "0.5"
    assert matchup.subject_age_seconds == 3600
    assert matchup.opponent_age_seconds == 3600
    assert matchup.causal_cutoff == T1
    assert matchup.published_at == T1_PUBLISHED
    assert _digest(matchup.payload(include_id=False)) == matchup.matchup_id

    base = OpportunityEvidence(
        evidence_id="canonical-market-evidence",
        observed_at=T2,
        causal_cutoff=T1,
        reproducibility_sha256=sha256(b"market-replay").hexdigest(),
    )
    bound = bind_sport_memory_to_opportunity_evidence(
        base,
        matchup,
        runtime=runtime,
    )
    assert bound.reproducibility_sha256 != base.reproducibility_sha256
    assert bound.observed_at == base.observed_at
    assert bound.causal_cutoff == matchup.causal_cutoff
    assert bound.truth is base.truth
    assert bound.execution_feasible is base.execution_feasible

    records = runtime.record_matchup_consumption(
        decision_id="decision-matchup-1",
        matchup=matchup,
        decision_cutoff=T2,
        consumed_at=T2_CONSUMED,
    )
    assert {record.memory_id for record in records} == {
        matchup.subject_memory_id,
        matchup.opponent_memory_id,
    }
    assert runtime.consumptions_for_decision("decision-matchup-1") == records

    reopened = SportMemoryRuntime(
        tmp_path / "sport-memory.json",
        _MatchupAuthority(),
        authority_generation_sha256=GENERATION,
    )
    assert reopened.consumptions_for_decision("decision-matchup-1") == records
    assert (
        reopened.matchup_as_of(
            "participant-a",
            "participant-b",
            _scope(),
            as_of=T2,
        )
        == matchup
    )


def test_historical_decision_cannot_rebind_participant_after_later_snapshot(
    tmp_path,
):
    runtime = _runtime(tmp_path)
    _materialize_pair(runtime, cutoff=T1, published_at=T1_PUBLISHED)
    earlier = runtime.matchup_as_of(
        "participant-a",
        "participant-b",
        _scope(),
        as_of=T2,
    )
    earlier_records = runtime.record_matchup_consumption(
        decision_id="decision-historical",
        matchup=earlier,
        decision_cutoff=T2,
        consumed_at=T2_CONSUMED,
    )

    _materialize_pair(runtime, cutoff=T3, published_at=T3_PUBLISHED)
    later = runtime.matchup_as_of(
        "participant-a",
        "participant-b",
        _scope(),
        as_of=T4,
    )
    assert later.subject_memory_id != earlier.subject_memory_id
    assert later.opponent_memory_id != earlier.opponent_memory_id

    substituted = replace(
        later,
        subject_memory_id=earlier.subject_memory_id,
        matchup_id="0" * 64,
    )
    substituted = replace(
        substituted,
        matchup_id=_digest(substituted.payload(include_id=False)),
    )
    with pytest.raises(
        SportMemoryError,
        match="not canonical latest-as-of",
    ):
        runtime.verify_matchup_evidence(substituted)

    with pytest.raises(
        SportMemoryError,
        match="participant memory semantic drift",
    ):
        runtime.record_consumption(
            decision_id="decision-historical",
            memory_id=later.subject_memory_id,
            decision_cutoff=T4,
            consumed_at=T4_CONSUMED,
            expected_scope=_scope(),
        )

    assert (
        runtime.consumptions_for_decision("decision-historical")
        == earlier_records
    )
    later_records = runtime.record_matchup_consumption(
        decision_id="decision-later",
        matchup=later,
        decision_cutoff=T4,
        consumed_at=T4_CONSUMED,
    )
    assert {record.memory_id for record in later_records} == {
        later.subject_memory_id,
        later.opponent_memory_id,
    }


def test_matchup_selection_and_binding_fail_closed_on_future_evidence(tmp_path):
    runtime = _runtime(tmp_path)
    _materialize_pair(runtime, cutoff=T1, published_at=T1_PUBLISHED)

    with pytest.raises(
        SportMemoryError,
        match="both causal participant memories",
    ):
        runtime.matchup_as_of(
            "participant-a",
            "participant-b",
            _scope(),
            as_of=T1,
        )

    matchup = runtime.matchup_as_of(
        "participant-a",
        "participant-b",
        _scope(),
        as_of=T2,
    )
    early_base = OpportunityEvidence(
        evidence_id="early-market-evidence",
        observed_at="2026-09-20T10:00:02Z",
        causal_cutoff=T1,
        reproducibility_sha256=sha256(b"early-replay").hexdigest(),
    )
    with pytest.raises(
        SportMemoryError,
        match="selected after opportunity evidence",
    ):
        bind_sport_memory_to_opportunity_evidence(
            early_base,
            matchup,
            runtime=runtime,
        )

    with pytest.raises(
        SportMemoryError,
        match="selected after decision cutoff",
    ):
        runtime.record_matchup_consumption(
            decision_id="decision-too-early",
            matchup=matchup,
            decision_cutoff="2026-09-20T10:04:59Z",
            consumed_at=T2_CONSUMED,
        )
