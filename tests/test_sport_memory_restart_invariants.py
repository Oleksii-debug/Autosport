from __future__ import annotations

from dataclasses import dataclass
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


class _LowLevelSportMemoryRuntime(_PublicSportMemoryRuntime):
    """Unit-test harness for deterministic restart semantics, not a product API."""

    def _require_durable_positive_authority(self) -> None:
        return None

    def materialize(self, *args, **kwargs):
        return _guard._ORIGINAL_MATERIALIZE(self, *args, **kwargs)

    def record_consumption(self, *args, **kwargs):
        return _guard._ORIGINAL_RECORD_CONSUMPTION(self, *args, **kwargs)


SportMemoryRuntime = _LowLevelSportMemoryRuntime


def _digest(payload: object) -> str:
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class _Authority:
    feature_published_at: str = "2026-09-20T10:00:01Z"
    feature_support: int = 2

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
            uncertainty="0.5",
            state=SnapshotState.SUPPORTED,
        )
        feature = FeatureSnapshot(
            snapshot_id=SHA_F,
            rating_snapshot_id=rating.snapshot_id,
            participant_entity_id=rating.participant_entity_id,
            sport_id=rating.sport_id,
            league_id=rating.league_id,
            market_context_id=rating.market_context_id,
            view=rating.view,
            causal_cutoff=rating.causal_cutoff,
            published_at=self.feature_published_at,
            input_digest=rating.input_digest,
            support=self.feature_support,
            effective_sample=rating.effective_sample,
            opponent_count=rating.opponent_count,
            last_observed_at="2026-09-19T10:00:00Z",
            age_seconds=86400,
            state=rating.state,
        )
        return rating, feature


def _scope() -> SportMemoryScope:
    return SportMemoryScope(
        sport_id="tennis",
        league_entity_id="atp",
        market_context_id="match-winner",
    )


def _runtime(tmp_path):
    path = tmp_path / "sport-memory.json"
    runtime = SportMemoryRuntime.initialize_pristine(
        path,
        _Authority(),
        authority_generation_sha256=SHA_3,
    )
    artifact = runtime.materialize(
        participant_entity_id="participant-1",
        scope=_scope(),
        causal_cutoff="2026-09-20T10:00:00Z",
        published_at="2026-09-20T10:00:01Z",
        code_sha256=SHA_C,
        dependency_sha256=SHA_D,
        view=IdentityView.AS_KNOWN_AT_DECISION,
    )
    return path, runtime, artifact


def test_later_feature_publication_cannot_be_hidden_by_rating_publication(tmp_path):
    runtime = SportMemoryRuntime.initialize_pristine(
        tmp_path / "sport-memory.json",
        _Authority(feature_published_at="2026-09-20T10:00:02Z"),
        authority_generation_sha256=SHA_3,
    )

    with pytest.raises(SportMemoryError, match="one coherent requested view"):
        runtime.materialize(
            participant_entity_id="participant-1",
            scope=_scope(),
            causal_cutoff="2026-09-20T10:00:00Z",
            published_at="2026-09-20T10:00:01Z",
            code_sha256=SHA_C,
            dependency_sha256=SHA_D,
        )


def test_rating_and_feature_support_must_describe_one_snapshot_pair(tmp_path):
    runtime = SportMemoryRuntime.initialize_pristine(
        tmp_path / "sport-memory.json",
        _Authority(feature_support=1),
        authority_generation_sha256=SHA_3,
    )

    with pytest.raises(SportMemoryError, match="one coherent requested view"):
        runtime.materialize(
            participant_entity_id="participant-1",
            scope=_scope(),
            causal_cutoff="2026-09-20T10:00:00Z",
            published_at="2026-09-20T10:00:01Z",
            code_sha256=SHA_C,
            dependency_sha256=SHA_D,
        )


def test_restart_replays_artifact_publication_invariant_after_recomputed_id(tmp_path):
    path, _, _ = _runtime(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    artifact = raw["artifacts"][0]
    artifact["published_at"] = "2026-09-20T09:59:59Z"
    artifact["memory_id"] = _digest({k: v for k, v in artifact.items() if k != "memory_id"})
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="published before causal cutoff"):
        SportMemoryRuntime(path, _Authority(), authority_generation_sha256=SHA_3)


def test_restart_rejects_bool_counter_even_with_recomputed_id(tmp_path):
    path, _, _ = _runtime(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    artifact = raw["artifacts"][0]
    artifact["support"] = True
    artifact["memory_id"] = _digest({k: v for k, v in artifact.items() if k != "memory_id"})
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="support must be a non-negative integer"):
        SportMemoryRuntime(path, _Authority(), authority_generation_sha256=SHA_3)


def test_restart_replays_consumption_causality_after_recomputed_id(tmp_path):
    path, runtime, artifact = _runtime(tmp_path)
    runtime.record_consumption(
        decision_id="decision-1",
        memory_id=artifact.memory_id,
        decision_cutoff="2026-09-20T10:00:01Z",
        consumed_at="2026-09-20T10:00:02Z",
        expected_scope=_scope(),
    )

    raw = json.loads(path.read_text(encoding="utf-8"))
    record = raw["consumptions"][0]
    record["consumed_at"] = "2026-09-20T10:00:00Z"
    record["consumption_id"] = _digest(
        {k: v for k, v in record.items() if k != "consumption_id"}
    )
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="cannot precede decision cutoff"):
        SportMemoryRuntime(path, _Authority(), authority_generation_sha256=SHA_3)


def test_restart_rejects_support_not_equal_to_input_count_after_recomputed_id(tmp_path):
    path, _, _ = _runtime(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    artifact = raw["artifacts"][0]
    artifact["support"] = 3
    artifact["memory_id"] = _digest({k: v for k, v in artifact.items() if k != "memory_id"})
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="support must equal input performance count"):
        SportMemoryRuntime(path, _Authority(), authority_generation_sha256=SHA_3)


def test_restart_rejects_effective_sample_not_bounded_opponent_support_after_recomputed_id(tmp_path):
    path, _, _ = _runtime(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    artifact = raw["artifacts"][0]
    artifact["effective_sample"] = 1
    artifact["memory_id"] = _digest({k: v for k, v in artifact.items() if k != "memory_id"})
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="effective_sample must equal bounded opponent support"):
        SportMemoryRuntime(path, _Authority(), authority_generation_sha256=SHA_3)


def test_restart_rejects_input_digest_drift_after_recomputed_memory_id(tmp_path):
    path, _, _ = _runtime(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    artifact = raw["artifacts"][0]
    artifact["input_digest"] = "9" * 64
    artifact["memory_id"] = _digest({k: v for k, v in artifact.items() if k != "memory_id"})
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="input_digest does not match input performance ids"):
        SportMemoryRuntime(path, _Authority(), authority_generation_sha256=SHA_3)


def test_restart_rejects_duplicate_input_ids_after_recomputed_memory_id(tmp_path):
    path, _, _ = _runtime(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    artifact = raw["artifacts"][0]
    artifact["input_performance_ids"] = [SHA_1, SHA_1]
    artifact["input_digest"] = _digest(artifact["input_performance_ids"])
    artifact["memory_id"] = _digest({k: v for k, v in artifact.items() if k != "memory_id"})
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="input_performance_ids must be unique"):
        SportMemoryRuntime(path, _Authority(), authority_generation_sha256=SHA_3)


def test_restart_rejects_age_drift_after_recomputed_memory_id(tmp_path):
    path, _, _ = _runtime(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    artifact = raw["artifacts"][0]
    artifact["age_seconds"] = 1
    artifact["memory_id"] = _digest({k: v for k, v in artifact.items() if k != "memory_id"})
    path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="age_seconds does not match causal cutoff"):
        SportMemoryRuntime(path, _Authority(), authority_generation_sha256=SHA_3)
