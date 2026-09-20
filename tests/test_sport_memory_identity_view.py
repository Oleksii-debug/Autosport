from __future__ import annotations

from dataclasses import dataclass
import json

import pytest

from autosport import _sport_memory_authority_guard as _guard
from autosport.opponent_intelligence import (
    FeatureSnapshot,
    IdentityView,
    RatingSnapshot,
    SnapshotState,
)
from autosport.sport_memory_runtime import (
    SportMemoryError,
    SportMemoryRuntime as _PublicSportMemoryRuntime,
    SportMemoryScope,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "055373d2d5916d7245990454c54ab62817bf787538ca4475c73a310b556aabef"
SHA_F = "f" * 64
SHA_1 = "1" * 64
SHA_2 = "2" * 64
SHA_3 = "3" * 64


@dataclass
class _ViewAuthority:
    returned_view: IdentityView
    requested_view: IdentityView | None = None

    def build_snapshots(self, **kwargs):
        self.requested_view = kwargs["view"]
        rating = RatingSnapshot(
            snapshot_id=SHA_A,
            participant_entity_id="participant-1",
            sport_id="tennis",
            league_id="atp",
            market_context_id="match-winner",
            view=self.returned_view,
            causal_cutoff="2026-09-20T10:00:00Z",
            published_at="2026-09-20T10:00:01Z",
            algorithm_family="bounded-mean-score",
            algorithm_version="mean-score-v1",
            config_sha256=SHA_B,
            min_support=2,
            max_age_seconds=2592000,
            code_sha256=SHA_C,
            dependency_sha256=SHA_D,
            predecessor_snapshot_ids=(),
            input_performance_ids=(SHA_1, SHA_2),
            input_digest=SHA_E,
            support=2,
            effective_sample=2,
            opponent_count=2,
            rating="0.75",
            uncertainty="0.7071067811865475",
            state=SnapshotState.SUPPORTED,
        )
        feature = FeatureSnapshot(
            snapshot_id=SHA_F,
            rating_snapshot_id=rating.snapshot_id,
            participant_entity_id=rating.participant_entity_id,
            sport_id=rating.sport_id,
            league_id=rating.league_id,
            market_context_id=rating.market_context_id,
            view=self.returned_view,
            causal_cutoff=rating.causal_cutoff,
            published_at=rating.published_at,
            input_digest=rating.input_digest,
            support=rating.support,
            effective_sample=rating.effective_sample,
            opponent_count=rating.opponent_count,
            last_observed_at="2026-09-19T10:00:00Z",
            age_seconds=86400,
            state=rating.state,
        )
        return rating, feature


class _LowLevelSportMemoryRuntime(_PublicSportMemoryRuntime):
    """Unit-test harness for deterministic identity-view core semantics."""

    def _require_durable_positive_authority(self) -> None:
        # Unit tests exercise the deterministic storage engine directly. Product
        # authority remains default-deny on the actual public runtime.
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


def _materialize(
    runtime: _PublicSportMemoryRuntime,
    *,
    view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
):
    return runtime.materialize(
        participant_entity_id="participant-1",
        scope=_scope(),
        causal_cutoff="2026-09-20T10:00:00Z",
        published_at="2026-09-20T10:00:01Z",
        code_sha256=SHA_C,
        dependency_sha256=SHA_D,
        view=view,
    )


def test_as_known_request_rejects_restated_research_snapshot_pair(tmp_path):
    authority = _ViewAuthority(IdentityView.RESTATED_RESEARCH)
    runtime = SportMemoryRuntime.initialize_pristine(
        tmp_path / "sport-memory.json",
        authority,
        authority_generation_sha256=SHA_3,
    )

    with pytest.raises(
        SportMemoryError,
        match="requested identity view",
    ):
        _materialize(runtime, view=IdentityView.AS_KNOWN_AT_DECISION)

    assert authority.requested_view is IdentityView.AS_KNOWN_AT_DECISION
    assert runtime.participant_history("participant-1", _scope()) == ()


def test_identity_view_is_bound_to_artifact_digest_and_restart_readback(tmp_path):
    path = tmp_path / "sport-memory.json"
    authority = _ViewAuthority(IdentityView.AS_KNOWN_AT_DECISION)
    runtime = SportMemoryRuntime.initialize_pristine(
        path,
        authority,
        authority_generation_sha256=SHA_3,
    )

    artifact = _materialize(runtime)
    assert artifact.identity_view is IdentityView.AS_KNOWN_AT_DECISION

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 2
    assert raw["artifacts"][0]["identity_view"] == "AS_KNOWN_AT_DECISION"

    reopened = SportMemoryRuntime(
        path,
        _ViewAuthority(IdentityView.AS_KNOWN_AT_DECISION),
        authority_generation_sha256=SHA_3,
    )
    assert reopened.get(artifact.memory_id).identity_view is IdentityView.AS_KNOWN_AT_DECISION
    assert reopened.participant_history("participant-1", _scope()) == (artifact,)


def test_tampering_persisted_identity_view_invalidates_artifact_digest(tmp_path):
    path = tmp_path / "sport-memory.json"
    runtime = SportMemoryRuntime.initialize_pristine(
        path,
        _ViewAuthority(IdentityView.AS_KNOWN_AT_DECISION),
        authority_generation_sha256=SHA_3,
    )
    _materialize(runtime)

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["artifacts"][0]["identity_view"] = "RESTATED_RESEARCH"
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="artifact digest mismatch"):
        SportMemoryRuntime(
            path,
            _ViewAuthority(IdentityView.RESTATED_RESEARCH),
            authority_generation_sha256=SHA_3,
        )


def test_restated_research_artifact_is_not_default_decision_view(tmp_path):
    runtime = SportMemoryRuntime.initialize_pristine(
        tmp_path / "sport-memory.json",
        _ViewAuthority(IdentityView.RESTATED_RESEARCH),
        authority_generation_sha256=SHA_3,
    )
    artifact = _materialize(runtime, view=IdentityView.RESTATED_RESEARCH)

    assert artifact.identity_view is IdentityView.RESTATED_RESEARCH
    assert runtime.participant_history("participant-1", _scope()) == ()
    assert runtime.last_causal_snapshot(
        "participant-1",
        _scope(),
        as_of="2026-09-20T10:00:02Z",
    ) is None
    assert runtime.participant_history(
        "participant-1",
        _scope(),
        view=IdentityView.RESTATED_RESEARCH,
    ) == (artifact,)

    with pytest.raises(SportMemoryError, match="identity view mismatch"):
        runtime.record_consumption(
            decision_id="decision-as-known",
            memory_id=artifact.memory_id,
            decision_cutoff="2026-09-20T10:00:02Z",
            consumed_at="2026-09-20T10:00:03Z",
            expected_scope=_scope(),
        )

    consumption = runtime.record_consumption(
        decision_id="decision-restated",
        memory_id=artifact.memory_id,
        decision_cutoff="2026-09-20T10:00:02Z",
        consumed_at="2026-09-20T10:00:03Z",
        expected_scope=_scope(),
        expected_view=IdentityView.RESTATED_RESEARCH,
    )
    assert consumption.memory_id == artifact.memory_id
