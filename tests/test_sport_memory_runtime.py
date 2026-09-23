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


@dataclass
class _FakeOpponentAuthority:
    cutoff: str = "2026-09-20T10:00:00Z"
    published: str = "2026-09-20T10:00:01Z"
    state: SnapshotState = SnapshotState.SUPPORTED

    def build_snapshots(self, **kwargs):
        assert kwargs["participant_entity_id"] == "participant-1"
        assert kwargs["sport_id"] == "tennis"
        assert kwargs["league_entity_id"] == "atp"
        assert kwargs["market_context_id"] == "match-winner"
        rating = RatingSnapshot(
            snapshot_id=SHA_A,
            participant_entity_id="participant-1",
            sport_id="tennis",
            league_id="atp",
            market_context_id="match-winner",
            view=IdentityView.AS_KNOWN_AT_DECISION,
            causal_cutoff=self.cutoff,
            published_at=self.published,
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
            rating="0.75" if self.state is SnapshotState.SUPPORTED else None,
            uncertainty=(
                "0.7071067811865475"
                if self.state is SnapshotState.SUPPORTED
                else None
            ),
            state=self.state,
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
    """Unit-test harness for deterministic core semantics, not a product API."""

    def _require_durable_positive_authority(self) -> None:
        # Unit tests exercise the deterministic storage engine directly. Product
        # authority remains default-deny on the actual public runtime.
        return None

    def materialize(self, *args, **kwargs):
        return _guard._ORIGINAL_MATERIALIZE(self, *args, **kwargs)

    def record_consumption(self, *args, **kwargs):
        return _guard._ORIGINAL_RECORD_CONSUMPTION(self, *args, **kwargs)


SportMemoryRuntime = _LowLevelSportMemoryRuntime


def _scope(
    *,
    sport_id: str = "tennis",
    league_entity_id: str = "atp",
    market_context_id: str = "match-winner",
) -> SportMemoryScope:
    return SportMemoryScope(
        sport_id=sport_id,
        league_entity_id=league_entity_id,
        market_context_id=market_context_id,
    )


def _materialize(runtime: _PublicSportMemoryRuntime):
    return runtime.materialize(
        participant_entity_id="participant-1",
        scope=_scope(),
        causal_cutoff="2026-09-20T10:00:00Z",
        published_at="2026-09-20T10:00:01Z",
        code_sha256=SHA_C,
        dependency_sha256=SHA_D,
    )


def _artifact_digest(raw: dict[str, object]) -> str:
    body = {key: value for key, value in raw.items() if key != "memory_id"}
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def test_public_base_runtime_cannot_mint_durable_positive_memory(tmp_path):
    path = tmp_path / "public-sport-memory.json"
    runtime = _PublicSportMemoryRuntime.initialize_pristine(
        path,
        _FakeOpponentAuthority(),
        authority_generation_sha256=SHA_3,
    )

    with pytest.raises(SportMemoryError, match="canonical bound authority"):
        _materialize(runtime)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["artifacts"] == []


def test_provider_specific_scope_fails_closed_until_canonical_provider_authority():
    for provider_id in ("provider-a", "provider-b"):
        with pytest.raises(SportMemoryError, match="provider-specific.*canonical provider authority"):
            SportMemoryScope(
                sport_id="tennis",
                league_entity_id="atp",
                market_context_id="match-winner",
                provider_id=provider_id,
            )

    scope = _scope()
    assert scope.provider_id is None
    assert scope.payload()["provider_id"] is None


def test_scope_identity_text_must_be_canonical():
    for field in ("sport_id", "league_entity_id", "market_context_id"):
        values = {
            "sport_id": "tennis",
            "league_entity_id": "atp",
            "market_context_id": "match-winner",
        }
        values[field] = f" {values[field]} "
        with pytest.raises(SportMemoryError, match=f"{field} must be canonical"):
            SportMemoryScope(**values)


def test_materialization_is_reference_only_idempotent_and_restart_safe(tmp_path):
    path = tmp_path / "sport-memory.json"
    runtime = SportMemoryRuntime.initialize_pristine(
        path,
        _FakeOpponentAuthority(),
        authority_generation_sha256=SHA_3,
    )

    first = _materialize(runtime)
    duplicate = _materialize(runtime)

    assert duplicate == first
    assert first.rating_snapshot_id == SHA_A
    assert first.feature_snapshot_id == SHA_F
    assert first.input_performance_ids == (SHA_1, SHA_2)
    assert first.support == 2
    assert first.state == "SUPPORTED"
    assert first.scope.provider_id is None

    reopened = SportMemoryRuntime(
        path,
        _FakeOpponentAuthority(),
        authority_generation_sha256=SHA_3,
    )
    assert reopened.get(first.memory_id) == first
    assert reopened.participant_history("participant-1", _scope()) == (first,)


def test_last_causal_snapshot_excludes_artifact_not_yet_published(tmp_path):
    runtime = SportMemoryRuntime.initialize_pristine(
        tmp_path / "sport-memory.json",
        _FakeOpponentAuthority(),
        authority_generation_sha256=SHA_3,
    )
    artifact = _materialize(runtime)

    assert (
        runtime.last_causal_snapshot(
            "participant-1", _scope(), as_of="2026-09-20T10:00:00Z"
        )
        is None
    )
    assert runtime.last_causal_snapshot(
        "participant-1", _scope(), as_of="2026-09-20T10:00:01Z"
    ) == artifact


def test_consumption_is_exact_scope_causal_and_idempotent(tmp_path):
    runtime = SportMemoryRuntime.initialize_pristine(
        tmp_path / "sport-memory.json",
        _FakeOpponentAuthority(),
        authority_generation_sha256=SHA_3,
    )
    artifact = _materialize(runtime)

    first = runtime.record_consumption(
        decision_id="decision-42",
        memory_id=artifact.memory_id,
        decision_cutoff="2026-09-20T10:00:01Z",
        consumed_at="2026-09-20T10:00:02Z",
        expected_scope=_scope(),
    )
    duplicate = runtime.record_consumption(
        decision_id="decision-42",
        memory_id=artifact.memory_id,
        decision_cutoff="2026-09-20T10:00:01Z",
        consumed_at="2026-09-20T10:00:02Z",
        expected_scope=_scope(),
    )
    assert duplicate == first
    assert runtime.consumptions_for_artifact(artifact.memory_id) == (first,)

    with pytest.raises(SportMemoryError, match="scope mismatch"):
        runtime.record_consumption(
            decision_id="decision-cross-domain",
            memory_id=artifact.memory_id,
            decision_cutoff="2026-09-20T10:00:01Z",
            consumed_at="2026-09-20T10:00:02Z",
            expected_scope=_scope(market_context_id="set-winner"),
        )

    with pytest.raises(SportMemoryError, match="unavailable"):
        runtime.record_consumption(
            decision_id="decision-too-early",
            memory_id=artifact.memory_id,
            decision_cutoff="2026-09-20T10:00:00Z",
            consumed_at="2026-09-20T10:00:02Z",
            expected_scope=_scope(),
        )


def test_restart_rejects_forged_provider_label_on_same_canonical_snapshot(tmp_path):
    path = tmp_path / "sport-memory.json"
    runtime = SportMemoryRuntime.initialize_pristine(
        path,
        _FakeOpponentAuthority(),
        authority_generation_sha256=SHA_3,
    )
    _materialize(runtime)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["artifacts"][0]["scope"]["provider_id"] = "provider-b"
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="provider-specific.*canonical provider authority"):
        SportMemoryRuntime(
            path,
            _FakeOpponentAuthority(),
            authority_generation_sha256=SHA_3,
        )


def test_restart_fails_closed_on_mixed_authority_generation(tmp_path):
    path = tmp_path / "sport-memory.json"
    runtime = SportMemoryRuntime.initialize_pristine(
        path,
        _FakeOpponentAuthority(),
        authority_generation_sha256=SHA_3,
    )
    _materialize(runtime)

    with pytest.raises(SportMemoryError, match="generation mismatch"):
        SportMemoryRuntime(
            path,
            _FakeOpponentAuthority(),
            authority_generation_sha256=SHA_B,
        )


def test_restart_rejects_self_consistent_artifact_from_another_generation(tmp_path):
    path = tmp_path / "sport-memory.json"
    runtime = SportMemoryRuntime.initialize_pristine(
        path,
        _FakeOpponentAuthority(),
        authority_generation_sha256=SHA_3,
    )
    _materialize(runtime)

    payload = json.loads(path.read_text(encoding="utf-8"))
    artifact = payload["artifacts"][0]
    artifact["authority_generation_sha256"] = SHA_B
    artifact["memory_id"] = _artifact_digest(artifact)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(SportMemoryError, match="artifact authority generation"):
        SportMemoryRuntime(
            path,
            _FakeOpponentAuthority(),
            authority_generation_sha256=SHA_3,
        )


def test_insufficient_history_remains_abstention_compatible(tmp_path):
    runtime = SportMemoryRuntime.initialize_pristine(
        tmp_path / "sport-memory.json",
        _FakeOpponentAuthority(state=SnapshotState.INSUFFICIENT),
        authority_generation_sha256=SHA_3,
    )
    artifact = _materialize(runtime)

    assert artifact.state == "INSUFFICIENT"
    assert artifact.rating is None
    assert artifact.uncertainty is None
