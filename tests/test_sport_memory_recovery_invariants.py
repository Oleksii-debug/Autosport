from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json

import pytest

from autosport.opponent_intelligence import (
    FeatureSnapshot,
    IdentityView,
    RatingSnapshot,
    SnapshotState,
)
from autosport.sport_memory_runtime import (
    SportMemoryError,
    SportMemoryRuntime,
    SportMemoryScope,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64
SHA_1 = "1" * 64
SHA_2 = "2" * 64
SHA_3 = "3" * 64


def _digest(payload: object) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def _scope() -> SportMemoryScope:
    return SportMemoryScope(
        sport_id="tennis",
        league_entity_id="atp",
        market_context_id="match-winner",
    )


@dataclass
class _PairAuthority:
    feature_overrides: dict[str, object] | None = None
    rating_overrides: dict[str, object] | None = None

    def build_snapshots(self, **kwargs):
        rating = RatingSnapshot(
            snapshot_id=SHA_A,
            participant_entity_id="participant-1",
            sport_id="tennis",
            league_id="atp",
            market_context_id="match-winner",
            view=kwargs["view"],
            causal_cutoff=kwargs["causal_cutoff"],
            published_at=kwargs["published_at"],
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
        if self.rating_overrides:
            rating = replace(rating, **self.rating_overrides)
        feature = FeatureSnapshot(
            snapshot_id=SHA_F,
            rating_snapshot_id=rating.snapshot_id,
            participant_entity_id=rating.participant_entity_id,
            sport_id=rating.sport_id,
            league_id=rating.league_id,
            market_context_id=rating.market_context_id,
            view=rating.view,
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
        if self.feature_overrides:
            feature = replace(feature, **self.feature_overrides)
        return rating, feature


def _runtime(tmp_path, authority=None) -> SportMemoryRuntime:
    return SportMemoryRuntime.initialize_pristine(
        tmp_path / "sport-memory.json",
        authority or _PairAuthority(),
        authority_generation_sha256=SHA_3,
    )


def _materialize(runtime: SportMemoryRuntime):
    return runtime.materialize(
        participant_entity_id="participant-1",
        scope=_scope(),
        causal_cutoff="2026-09-20T10:00:00Z",
        published_at="2026-09-20T10:00:01Z",
        code_sha256=SHA_C,
        dependency_sha256=SHA_D,
        view=IdentityView.AS_KNOWN_AT_DECISION,
    )


def _rewrite_artifact_id(artifact: dict[str, object]) -> None:
    artifact["memory_id"] = _digest(
        {key: value for key, value in artifact.items() if key != "memory_id"}
    )


def _rewrite_consumption_id(consumption: dict[str, object]) -> None:
    consumption["consumption_id"] = _digest(
        {key: value for key, value in consumption.items() if key != "consumption_id"}
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"support": 3},
        {"effective_sample": 1},
        {"opponent_count": 1},
        {"state": SnapshotState.INSUFFICIENT},
    ],
)
def test_materialize_rejects_rating_feature_semantic_drift(tmp_path, overrides):
    runtime = _runtime(tmp_path, _PairAuthority(feature_overrides=overrides))

    with pytest.raises(SportMemoryError, match="snapshot pair semantic state mismatch"):
        _materialize(runtime)


def test_materialize_rejects_returned_publication_drift(tmp_path):
    runtime = _runtime(
        tmp_path,
        _PairAuthority(feature_overrides={"published_at": "2026-09-20T10:00:02Z"}),
    )

    with pytest.raises(SportMemoryError, match="requested scope"):
        _materialize(runtime)


def test_restart_rejects_recomputed_artifact_with_pre_cutoff_publication(tmp_path):
    runtime = _runtime(tmp_path)
    _materialize(runtime)
    path = tmp_path / "sport-memory.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["artifacts"][0]["published_at"] = "2026-09-20T09:59:59Z"
    _rewrite_artifact_id(raw["artifacts"][0])
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="cannot be published before causal cutoff"):
        SportMemoryRuntime(path, _PairAuthority(), authority_generation_sha256=SHA_3)


def test_restart_rejects_bool_counter_even_with_recomputed_artifact_id(tmp_path):
    runtime = _runtime(tmp_path)
    _materialize(runtime)
    path = tmp_path / "sport-memory.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["artifacts"][0]["support"] = True
    _rewrite_artifact_id(raw["artifacts"][0])
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="support must be a non-negative integer"):
        SportMemoryRuntime(path, _PairAuthority(), authority_generation_sha256=SHA_3)


def test_restart_rejects_recomputed_artifact_with_incoherent_age(tmp_path):
    runtime = _runtime(tmp_path)
    _materialize(runtime)
    path = tmp_path / "sport-memory.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["artifacts"][0]["age_seconds"] = 1
    _rewrite_artifact_id(raw["artifacts"][0])
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="age_seconds does not match causal cutoff"):
        SportMemoryRuntime(path, _PairAuthority(), authority_generation_sha256=SHA_3)


def test_restart_replays_consumption_semantics_after_recomputed_id(tmp_path):
    runtime = _runtime(tmp_path)
    artifact = _materialize(runtime)
    runtime.record_consumption(
        decision_id="decision-42",
        memory_id=artifact.memory_id,
        decision_cutoff="2026-09-20T10:00:01Z",
        consumed_at="2026-09-20T10:00:02Z",
        expected_scope=_scope(),
    )
    path = tmp_path / "sport-memory.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["consumptions"][0]["consumed_at"] = "2026-09-20T10:00:00Z"
    _rewrite_consumption_id(raw["consumptions"][0])
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="consumption cannot precede decision cutoff"):
        SportMemoryRuntime(path, _PairAuthority(), authority_generation_sha256=SHA_3)
