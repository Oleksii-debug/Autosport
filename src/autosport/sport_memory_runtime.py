from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Protocol

from .integrity import atomic_write_json
from .opponent_intelligence import FeatureSnapshot, IdentityView, RatingSnapshot


class SportMemoryError(ValueError):
    """Raised when sport-memory evidence is inconsistent or non-causal."""


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise SportMemoryError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha256(name: str, value: object) -> str:
    text = _text(name, value)
    if len(text) != 64 or text != text.lower() or any(c not in "0123456789abcdef" for c in text):
        raise SportMemoryError(f"{name} must be a lowercase sha256 hex digest")
    return text


def _instant(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        instant = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SportMemoryError(f"{name} must be an ISO-8601 timestamp") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise SportMemoryError(f"{name} must include an explicit timezone")
    return instant.astimezone(timezone.utc)


def _digest(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SportMemoryScope:
    sport_id: str
    league_entity_id: str
    market_context_id: str
    provider_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("sport_id", "league_entity_id", "market_context_id"):
            _text(name, getattr(self, name))
        if self.provider_id is not None:
            raise SportMemoryError(
                "provider-qualified scope is unresolved by canonical opponent snapshots"
            )

    def payload(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SportMemoryArtifact:
    memory_id: str
    participant_entity_id: str
    scope: SportMemoryScope
    causal_cutoff: str
    published_at: str
    rating_snapshot_id: str
    feature_snapshot_id: str
    input_digest: str
    input_performance_ids: tuple[str, ...]
    support: int
    effective_sample: int
    opponent_count: int
    rating: str | None
    uncertainty: str | None
    state: str
    last_observed_at: str | None
    age_seconds: int | None
    authority_generation_sha256: str

    def payload(self, *, include_id: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "participant_entity_id": self.participant_entity_id,
            "scope": self.scope.payload(),
            "causal_cutoff": self.causal_cutoff,
            "published_at": self.published_at,
            "rating_snapshot_id": self.rating_snapshot_id,
            "feature_snapshot_id": self.feature_snapshot_id,
            "input_digest": self.input_digest,
            "input_performance_ids": list(self.input_performance_ids),
            "support": self.support,
            "effective_sample": self.effective_sample,
            "opponent_count": self.opponent_count,
            "rating": self.rating,
            "uncertainty": self.uncertainty,
            "state": self.state,
            "last_observed_at": self.last_observed_at,
            "age_seconds": self.age_seconds,
            "authority_generation_sha256": self.authority_generation_sha256,
        }
        if include_id:
            result["memory_id"] = self.memory_id
        return result


@dataclass(frozen=True, slots=True)
class DecisionMemoryConsumption:
    consumption_id: str
    decision_id: str
    memory_id: str
    decision_cutoff: str
    consumed_at: str

    def payload(self, *, include_id: bool = True) -> dict[str, str]:
        result = {
            "decision_id": self.decision_id,
            "memory_id": self.memory_id,
            "decision_cutoff": self.decision_cutoff,
            "consumed_at": self.consumed_at,
        }
        if include_id:
            result["consumption_id"] = self.consumption_id
        return result


class OpponentSnapshotAuthority(Protocol):
    def build_snapshots(
        self,
        *,
        participant_entity_id: str,
        sport_id: str,
        league_entity_id: str,
        market_context_id: str,
        causal_cutoff: str,
        published_at: str,
        code_sha256: str,
        dependency_sha256: str,
        view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
        min_support: int = 2,
        max_age_seconds: int = 30 * 24 * 60 * 60,
        algorithm_version: str = "mean-score-v1",
    ) -> tuple[RatingSnapshot, FeatureSnapshot]: ...


class SportMemoryRuntime:
    """Durable reference bridge from canonical opponent snapshots to decisions.

    Provider-qualified scope deliberately fails closed until the canonical snapshot
    authority itself exposes provider provenance; callers cannot self-attest it.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Path, opponent_authority: OpponentSnapshotAuthority, *, authority_generation_sha256: str) -> None:
        self.path = Path(path)
        self.opponent_authority = opponent_authority
        self.authority_generation_sha256 = _sha256("authority_generation_sha256", authority_generation_sha256)
        self._artifacts: dict[str, SportMemoryArtifact] = {}
        self._consumptions: dict[str, DecisionMemoryConsumption] = {}
        self._decision_consumptions: dict[str, str] = {}
        if self.path.exists():
            self._load()

    @classmethod
    def initialize_pristine(cls, path: Path, opponent_authority: OpponentSnapshotAuthority, *, authority_generation_sha256: str) -> "SportMemoryRuntime":
        runtime = cls(path, opponent_authority, authority_generation_sha256=authority_generation_sha256)
        if runtime.path.exists():
            raise SportMemoryError("sport memory runtime already exists")
        runtime._persist()
        return runtime

    def materialize(
        self,
        *,
        participant_entity_id: str,
        scope: SportMemoryScope,
        causal_cutoff: str,
        published_at: str,
        code_sha256: str,
        dependency_sha256: str,
        view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
        min_support: int = 2,
        max_age_seconds: int = 30 * 24 * 60 * 60,
        algorithm_version: str = "mean-score-v1",
    ) -> SportMemoryArtifact:
        participant = _text("participant_entity_id", participant_entity_id)
        cutoff = _text("causal_cutoff", causal_cutoff)
        publication = _text("published_at", published_at)
        if _instant("published_at", publication) < _instant("causal_cutoff", cutoff):
            raise SportMemoryError("memory cannot be published before causal cutoff")
        if not isinstance(scope, SportMemoryScope):
            raise TypeError("scope must be SportMemoryScope")
        rating, feature = self.opponent_authority.build_snapshots(
            participant_entity_id=participant,
            sport_id=scope.sport_id,
            league_entity_id=scope.league_entity_id,
            market_context_id=scope.market_context_id,
            causal_cutoff=cutoff,
            published_at=publication,
            code_sha256=code_sha256,
            dependency_sha256=dependency_sha256,
            view=view,
            min_support=min_support,
            max_age_seconds=max_age_seconds,
            algorithm_version=algorithm_version,
        )
        self._validate_snapshot_pair(rating, feature, participant, scope, cutoff)
        payload: dict[str, object] = {
            "participant_entity_id": participant,
            "scope": scope.payload(),
            "causal_cutoff": rating.causal_cutoff,
            "published_at": rating.published_at,
            "rating_snapshot_id": rating.snapshot_id,
            "feature_snapshot_id": feature.snapshot_id,
            "input_digest": rating.input_digest,
            "input_performance_ids": list(rating.input_performance_ids),
            "support": rating.support,
            "effective_sample": rating.effective_sample,
            "opponent_count": rating.opponent_count,
            "rating": rating.rating,
            "uncertainty": rating.uncertainty,
            "state": rating.state.value,
            "last_observed_at": feature.last_observed_at,
            "age_seconds": feature.age_seconds,
            "authority_generation_sha256": self.authority_generation_sha256,
        }
        memory_id = _digest(payload)
        if memory_id in self._artifacts:
            return self._artifacts[memory_id]
        artifact = SportMemoryArtifact(
            memory_id=memory_id, participant_entity_id=participant, scope=scope,
            causal_cutoff=rating.causal_cutoff, published_at=rating.published_at,
            rating_snapshot_id=rating.snapshot_id, feature_snapshot_id=feature.snapshot_id,
            input_digest=rating.input_digest, input_performance_ids=tuple(rating.input_performance_ids),
            support=rating.support, effective_sample=rating.effective_sample, opponent_count=rating.opponent_count,
            rating=rating.rating, uncertainty=rating.uncertainty, state=rating.state.value,
            last_observed_at=feature.last_observed_at, age_seconds=feature.age_seconds,
            authority_generation_sha256=self.authority_generation_sha256,
        )
        self._artifacts[memory_id] = artifact
        try:
            self._persist()
        except Exception:
            self._artifacts.pop(memory_id, None)
            raise
        return artifact

    @staticmethod
    def _validate_snapshot_pair(rating: RatingSnapshot, feature: FeatureSnapshot, participant_entity_id: str, scope: SportMemoryScope, causal_cutoff: str) -> None:
        if feature.rating_snapshot_id != rating.snapshot_id:
            raise SportMemoryError("feature snapshot does not reference rating snapshot")
        if not all((
            rating.participant_entity_id == participant_entity_id,
            feature.participant_entity_id == participant_entity_id,
            rating.sport_id == scope.sport_id,
            feature.sport_id == scope.sport_id,
            rating.league_id == scope.league_entity_id,
            feature.league_id == scope.league_entity_id,
            rating.market_context_id == scope.market_context_id,
            feature.market_context_id == scope.market_context_id,
            rating.causal_cutoff == causal_cutoff,
            feature.causal_cutoff == causal_cutoff,
            feature.input_digest == rating.input_digest,
        )):
            raise SportMemoryError("canonical snapshot pair does not match requested scope")

    def get(self, memory_id: str) -> SportMemoryArtifact:
        key = _sha256("memory_id", memory_id)
        try:
            return self._artifacts[key]
        except KeyError as exc:
            raise SportMemoryError("unknown sport memory artifact") from exc

    def participant_history(self, participant_entity_id: str, scope: SportMemoryScope) -> tuple[SportMemoryArtifact, ...]:
        participant = _text("participant_entity_id", participant_entity_id)
        if not isinstance(scope, SportMemoryScope):
            raise TypeError("scope must be SportMemoryScope")
        records = (a for a in self._artifacts.values() if a.participant_entity_id == participant and a.scope == scope)
        return tuple(sorted(records, key=lambda a: (_instant("causal_cutoff", a.causal_cutoff), _instant("published_at", a.published_at), a.memory_id)))

    def last_causal_snapshot(self, participant_entity_id: str, scope: SportMemoryScope, *, as_of: str) -> SportMemoryArtifact | None:
        instant = _instant("as_of", as_of)
        candidates = [a for a in self.participant_history(participant_entity_id, scope) if _instant("causal_cutoff", a.causal_cutoff) <= instant and _instant("published_at", a.published_at) <= instant]
        return max(candidates, key=lambda a: (_instant("causal_cutoff", a.causal_cutoff), _instant("published_at", a.published_at), a.memory_id)) if candidates else None

    def record_consumption(self, *, decision_id: str, memory_id: str, decision_cutoff: str, consumed_at: str, expected_scope: SportMemoryScope) -> DecisionMemoryConsumption:
        decision = _text("decision_id", decision_id)
        artifact = self.get(memory_id)
        if not isinstance(expected_scope, SportMemoryScope):
            raise TypeError("expected_scope must be SportMemoryScope")
        if artifact.scope != expected_scope:
            raise SportMemoryError("sport memory scope mismatch; refusing cross-domain reuse")
        cutoff = _text("decision_cutoff", decision_cutoff)
        consumed = _text("consumed_at", consumed_at)
        cutoff_instant = _instant("decision_cutoff", cutoff)
        if _instant("causal_cutoff", artifact.causal_cutoff) > cutoff_instant:
            raise SportMemoryError("future causal snapshot cannot be consumed by decision")
        if _instant("published_at", artifact.published_at) > cutoff_instant:
            raise SportMemoryError("snapshot unavailable at decision cutoff")
        if _instant("consumed_at", consumed) < cutoff_instant:
            raise SportMemoryError("consumption cannot precede decision cutoff")
        existing_id = self._decision_consumptions.get(decision)
        if existing_id is not None:
            existing = self._consumptions[existing_id]
            if (existing.memory_id, existing.decision_cutoff, existing.consumed_at) != (artifact.memory_id, cutoff, consumed):
                raise SportMemoryError("decision consumption semantic drift")
            return existing
        payload = {"decision_id": decision, "memory_id": artifact.memory_id, "decision_cutoff": cutoff, "consumed_at": consumed}
        record = DecisionMemoryConsumption(consumption_id=_digest(payload), **payload)
        self._consumptions[record.consumption_id] = record
        self._decision_consumptions[decision] = record.consumption_id
        try:
            self._persist()
        except Exception:
            self._decision_consumptions.pop(decision, None)
            self._consumptions.pop(record.consumption_id, None)
            raise
        return record

    def consumptions_for_artifact(self, memory_id: str) -> tuple[DecisionMemoryConsumption, ...]:
        artifact = self.get(memory_id)
        records = (r for r in self._consumptions.values() if r.memory_id == artifact.memory_id)
        return tuple(sorted(records, key=lambda r: (_instant("decision_cutoff", r.decision_cutoff), r.consumption_id)))

    def _persist(self) -> None:
        atomic_write_json(self.path, {
            "schema_version": self.SCHEMA_VERSION,
            "authority_generation_sha256": self.authority_generation_sha256,
            "artifacts": [a.payload() for a in sorted(self._artifacts.values(), key=lambda x: x.memory_id)],
            "consumptions": [r.payload() for r in sorted(self._consumptions.values(), key=lambda x: x.consumption_id)],
        })

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if type(payload) is not dict or payload.get("schema_version") != self.SCHEMA_VERSION:
                raise SportMemoryError("unsupported sport memory schema version")
            generation = _sha256("authority_generation_sha256", payload["authority_generation_sha256"])
            if generation != self.authority_generation_sha256:
                raise SportMemoryError("authority generation mismatch; refusing mixed-generation restart")
            artifacts = payload["artifacts"]
            consumptions = payload["consumptions"]
            if type(artifacts) is not list or type(consumptions) is not list:
                raise SportMemoryError("invalid sport memory checkpoint")
            for raw in artifacts:
                artifact = self._artifact_from_raw(raw, generation)
                if artifact.memory_id in self._artifacts:
                    raise SportMemoryError("duplicate sport memory artifact")
                self._artifacts[artifact.memory_id] = artifact
            for raw in consumptions:
                record = self._consumption_from_raw(raw)
                if record.memory_id not in self._artifacts:
                    raise SportMemoryError("consumption references unknown memory artifact")
                if record.consumption_id in self._consumptions:
                    raise SportMemoryError("duplicate sport memory consumption")
                if record.decision_id in self._decision_consumptions:
                    raise SportMemoryError("duplicate decision consumption")
                self._consumptions[record.consumption_id] = record
                self._decision_consumptions[record.decision_id] = record.consumption_id
        except SportMemoryError:
            raise
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, OverflowError) as exc:
            raise SportMemoryError("invalid sport memory checkpoint") from exc

    @staticmethod
    def _artifact_from_raw(raw: object, generation: str) -> SportMemoryArtifact:
        if type(raw) is not dict:
            raise SportMemoryError("invalid sport memory artifact")
        r = raw
        artifact = SportMemoryArtifact(
            memory_id=_sha256("memory_id", r["memory_id"]),
            participant_entity_id=_text("participant_entity_id", r["participant_entity_id"]),
            scope=SportMemoryScope(**r["scope"]),
            causal_cutoff=_text("causal_cutoff", r["causal_cutoff"]),
            published_at=_text("published_at", r["published_at"]),
            rating_snapshot_id=_sha256("rating_snapshot_id", r["rating_snapshot_id"]),
            feature_snapshot_id=_sha256("feature_snapshot_id", r["feature_snapshot_id"]),
            input_digest=_sha256("input_digest", r["input_digest"]),
            input_performance_ids=tuple(_sha256("input_performance_id", v) for v in r["input_performance_ids"]),
            support=int(r["support"]), effective_sample=int(r["effective_sample"]), opponent_count=int(r["opponent_count"]),
            rating=r["rating"], uncertainty=r["uncertainty"], state=_text("state", r["state"]),
            last_observed_at=r["last_observed_at"], age_seconds=r["age_seconds"], authority_generation_sha256=generation,
        )
        if _digest(artifact.payload(include_id=False)) != artifact.memory_id:
            raise SportMemoryError("sport memory artifact digest mismatch")
        return artifact

    @staticmethod
    def _consumption_from_raw(raw: object) -> DecisionMemoryConsumption:
        if type(raw) is not dict:
            raise SportMemoryError("invalid sport memory consumption")
        r = raw
        record = DecisionMemoryConsumption(
            consumption_id=_sha256("consumption_id", r["consumption_id"]),
            decision_id=_text("decision_id", r["decision_id"]),
            memory_id=_sha256("memory_id", r["memory_id"]),
            decision_cutoff=_text("decision_cutoff", r["decision_cutoff"]),
            consumed_at=_text("consumed_at", r["consumed_at"]),
        )
        if _digest(record.payload(include_id=False)) != record.consumption_id:
            raise SportMemoryError("sport memory consumption digest mismatch")
        return record
