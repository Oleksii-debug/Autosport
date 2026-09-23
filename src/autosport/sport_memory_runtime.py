from __future__ import annotations

from dataclasses import asdict, dataclass, replace
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
        raise SportMemoryError(f"{name} must be canonical non-empty string")
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


def _nonnegative_int(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise SportMemoryError(f"{name} must be a non-negative integer")
    return value


def _optional_text(name: str, value: object) -> str | None:
    if value is None:
        return None
    return _text(name, value)


def _digest(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return sha256(raw.encode("utf-8")).hexdigest()


def _time_text_from_instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _identity_view_from_raw(value: object) -> IdentityView:
    text = _text("identity_view", value)
    try:
        return IdentityView(text)
    except ValueError as exc:
        raise SportMemoryError("identity_view is not a supported causal view") from exc


def _require_identity_view(value: object, *, name: str = "view") -> IdentityView:
    if not isinstance(value, IdentityView):
        raise TypeError(f"{name} must be IdentityView")
    return value


def _require_exact_keys(name: str, raw: object, expected: set[str]) -> dict[str, object]:
    if type(raw) is not dict or set(raw) != expected:
        raise SportMemoryError(f"invalid {name} schema")
    return raw


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
                "provider-specific scope requires canonical provider authority"
            )

    def payload(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SportMemoryArtifact:
    memory_id: str
    participant_entity_id: str
    scope: SportMemoryScope
    identity_view: IdentityView
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
            "identity_view": self.identity_view.value,
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
class SportMemoryMatchupEvidence:
    """Exact immutable participant/opponent memory selected for one decision time."""

    matchup_id: str
    subject_memory_id: str
    opponent_memory_id: str
    subject_participant_entity_id: str
    opponent_participant_entity_id: str
    scope: SportMemoryScope
    identity_view: IdentityView
    as_of: str
    causal_cutoff: str
    published_at: str
    subject_support: int
    opponent_support: int
    subject_uncertainty: str
    opponent_uncertainty: str
    subject_age_seconds: int
    opponent_age_seconds: int
    authority_generation_sha256: str

    def __post_init__(self) -> None:
        _sha256("matchup_id", self.matchup_id)
        _sha256("subject_memory_id", self.subject_memory_id)
        _sha256("opponent_memory_id", self.opponent_memory_id)
        if self.subject_memory_id == self.opponent_memory_id:
            raise SportMemoryError("matchup memory identities must be distinct")
        subject = _text(
            "subject_participant_entity_id",
            self.subject_participant_entity_id,
        )
        opponent = _text(
            "opponent_participant_entity_id",
            self.opponent_participant_entity_id,
        )
        if subject == opponent:
            raise SportMemoryError("matchup participants must be distinct")
        if type(self.scope) is not SportMemoryScope:
            raise SportMemoryError("matchup scope must be canonical SportMemoryScope")
        view = _require_identity_view(self.identity_view, name="identity_view")
        if view is not IdentityView.AS_KNOWN_AT_DECISION:
            raise SportMemoryError(
                "decision-time matchup requires AS_KNOWN_AT_DECISION view"
            )
        as_of = _instant("matchup as_of", self.as_of)
        cutoff = _instant("matchup causal_cutoff", self.causal_cutoff)
        published = _instant("matchup published_at", self.published_at)
        if cutoff > as_of:
            raise SportMemoryError("matchup causal cutoff cannot exceed as_of")
        if published > as_of:
            raise SportMemoryError("matchup publication cannot exceed as_of")
        if published < cutoff:
            raise SportMemoryError(
                "matchup publication cannot precede causal cutoff"
            )
        for name in ("subject_support", "opponent_support"):
            if _nonnegative_int(name, getattr(self, name)) <= 0:
                raise SportMemoryError(f"{name} must be positive")
        _text("subject_uncertainty", self.subject_uncertainty)
        _text("opponent_uncertainty", self.opponent_uncertainty)
        _nonnegative_int("subject_age_seconds", self.subject_age_seconds)
        _nonnegative_int("opponent_age_seconds", self.opponent_age_seconds)
        _sha256(
            "authority_generation_sha256",
            self.authority_generation_sha256,
        )

    def payload(self, *, include_id: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "subject_memory_id": self.subject_memory_id,
            "opponent_memory_id": self.opponent_memory_id,
            "subject_participant_entity_id": self.subject_participant_entity_id,
            "opponent_participant_entity_id": self.opponent_participant_entity_id,
            "scope": self.scope.payload(),
            "identity_view": self.identity_view.value,
            "as_of": self.as_of,
            "causal_cutoff": self.causal_cutoff,
            "published_at": self.published_at,
            "subject_support": self.subject_support,
            "opponent_support": self.opponent_support,
            "subject_uncertainty": self.subject_uncertainty,
            "opponent_uncertainty": self.opponent_uncertainty,
            "subject_age_seconds": self.subject_age_seconds,
            "opponent_age_seconds": self.opponent_age_seconds,
            "authority_generation_sha256": self.authority_generation_sha256,
        }
        if include_id:
            result["matchup_id"] = self.matchup_id
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

    SCHEMA_VERSION = 2

    _CHECKPOINT_KEYS = {
        "schema_version",
        "authority_generation_sha256",
        "artifacts",
        "consumptions",
    }
    _ARTIFACT_KEYS = {
        "memory_id",
        "participant_entity_id",
        "scope",
        "identity_view",
        "causal_cutoff",
        "published_at",
        "rating_snapshot_id",
        "feature_snapshot_id",
        "input_digest",
        "input_performance_ids",
        "support",
        "effective_sample",
        "opponent_count",
        "rating",
        "uncertainty",
        "state",
        "last_observed_at",
        "age_seconds",
        "authority_generation_sha256",
    }
    _CONSUMPTION_KEYS = {
        "consumption_id",
        "decision_id",
        "memory_id",
        "decision_cutoff",
        "consumed_at",
    }

    def __init__(self, path: Path, opponent_authority: OpponentSnapshotAuthority, *, authority_generation_sha256: str) -> None:
        self.path = Path(path)
        self.opponent_authority = opponent_authority
        self.authority_generation_sha256 = _sha256("authority_generation_sha256", authority_generation_sha256)
        self._artifacts: dict[str, SportMemoryArtifact] = {}
        self._consumptions: dict[str, DecisionMemoryConsumption] = {}
        # A portfolio decision may consume several participant memories. The
        # durable uniqueness key is therefore (decision_id, memory_id), while
        # same-participant/scope/view rebinding remains forbidden below.
        self._decision_consumptions: dict[tuple[str, str], str] = {}
        if self.path.exists():
            self._load()

    @classmethod
    def initialize_pristine(cls, path: Path, opponent_authority: OpponentSnapshotAuthority, *, authority_generation_sha256: str) -> "SportMemoryRuntime":
        runtime = cls(path, opponent_authority, authority_generation_sha256=authority_generation_sha256)
        if runtime.path.exists():
            raise SportMemoryError("sport memory runtime already exists")
        runtime._persist()
        return runtime

    def _require_durable_positive_authority(self) -> None:
        # Import lazily to avoid a module cycle while making the production write
        # boundary exact: only the concrete checkpoint-bound runtime may emit a
        # durable positive sport-memory artifact. Low-level fake-authority seams
        # remain test-only and cannot authorize this public production path.
        from .sport_memory_checkpoint import BoundSportMemoryRuntime

        if type(self) is not BoundSportMemoryRuntime:
            raise SportMemoryError(
                "durable positive sport memory requires checkpoint-bound canonical authority"
            )

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
        self._require_durable_positive_authority()
        participant = _text("participant_entity_id", participant_entity_id)
        cutoff = _text("causal_cutoff", causal_cutoff)
        publication = _text("published_at", published_at)
        requested_view = _require_identity_view(view)
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
            view=requested_view,
            min_support=min_support,
            max_age_seconds=max_age_seconds,
            algorithm_version=algorithm_version,
        )
        self._validate_snapshot_pair(
            rating,
            feature,
            participant,
            scope,
            cutoff,
            publication,
            requested_view,
        )
        payload: dict[str, object] = {
            "participant_entity_id": participant,
            "scope": scope.payload(),
            "identity_view": requested_view.value,
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
            memory_id=memory_id,
            participant_entity_id=participant,
            scope=scope,
            identity_view=requested_view,
            causal_cutoff=rating.causal_cutoff,
            published_at=rating.published_at,
            rating_snapshot_id=rating.snapshot_id,
            feature_snapshot_id=feature.snapshot_id,
            input_digest=rating.input_digest,
            input_performance_ids=tuple(rating.input_performance_ids),
            support=rating.support,
            effective_sample=rating.effective_sample,
            opponent_count=rating.opponent_count,
            rating=rating.rating,
            uncertainty=rating.uncertainty,
            state=rating.state.value,
            last_observed_at=feature.last_observed_at,
            age_seconds=feature.age_seconds,
            authority_generation_sha256=self.authority_generation_sha256,
        )
        self._validate_artifact(artifact)
        self._artifacts[memory_id] = artifact
        try:
            self._persist()
        except Exception:
            self._artifacts.pop(memory_id, None)
            raise
        return artifact

    @staticmethod
    def _validate_snapshot_pair(
        rating: RatingSnapshot,
        feature: FeatureSnapshot,
        participant_entity_id: str,
        scope: SportMemoryScope,
        causal_cutoff: str,
        published_at: str,
        requested_view: IdentityView,
    ) -> None:
        if type(rating) is not RatingSnapshot or type(feature) is not FeatureSnapshot:
            raise SportMemoryError(
                "canonical snapshot authority returned unexpected snapshot type"
            )
        if rating.view is not requested_view or feature.view is not requested_view:
            raise SportMemoryError(
                "canonical snapshot pair does not match requested identity view"
            )
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
            rating.published_at == published_at,
            feature.published_at == rating.published_at,
            feature.input_digest == rating.input_digest,
            feature.support == rating.support,
            feature.effective_sample == rating.effective_sample,
            feature.opponent_count == rating.opponent_count,
            feature.state is rating.state,
        )):
            raise SportMemoryError("canonical snapshot pair is not one coherent requested view")
        if _instant("snapshot published_at", rating.published_at) < _instant(
            "snapshot causal_cutoff", rating.causal_cutoff
        ):
            raise SportMemoryError("canonical snapshot cannot be published before causal cutoff")

    @staticmethod
    def _validate_artifact(artifact: SportMemoryArtifact) -> None:
        if type(artifact) is not SportMemoryArtifact:
            raise SportMemoryError("invalid sport memory artifact")
        _sha256("memory_id", artifact.memory_id)
        _text("participant_entity_id", artifact.participant_entity_id)
        if type(artifact.scope) is not SportMemoryScope:
            raise SportMemoryError("invalid sport memory scope")
        _require_identity_view(artifact.identity_view, name="identity_view")
        cutoff = _instant("causal_cutoff", artifact.causal_cutoff)
        published = _instant("published_at", artifact.published_at)
        if published < cutoff:
            raise SportMemoryError("memory cannot be published before causal cutoff")
        _sha256("rating_snapshot_id", artifact.rating_snapshot_id)
        _sha256("feature_snapshot_id", artifact.feature_snapshot_id)
        _sha256("input_digest", artifact.input_digest)
        if type(artifact.input_performance_ids) is not tuple:
            raise SportMemoryError("input_performance_ids must be an immutable tuple")
        for value in artifact.input_performance_ids:
            _sha256("input_performance_id", value)
        if len(set(artifact.input_performance_ids)) != len(artifact.input_performance_ids):
            raise SportMemoryError("input_performance_ids must be unique")
        if _digest(list(artifact.input_performance_ids)) != artifact.input_digest:
            raise SportMemoryError("input_digest does not match input performance ids")
        support = _nonnegative_int("support", artifact.support)
        effective_sample = _nonnegative_int("effective_sample", artifact.effective_sample)
        opponent_count = _nonnegative_int("opponent_count", artifact.opponent_count)
        if support != len(artifact.input_performance_ids):
            raise SportMemoryError("support must equal input performance count")
        if effective_sample != min(support, opponent_count):
            raise SportMemoryError("effective_sample must equal bounded opponent support")
        state = _text("state", artifact.state)
        if state not in {"SUPPORTED", "INSUFFICIENT"}:
            raise SportMemoryError("unsupported sport memory state")
        if artifact.rating is not None:
            _text("rating", artifact.rating)
        if artifact.uncertainty is not None:
            _text("uncertainty", artifact.uncertainty)
        if state == "SUPPORTED" and (artifact.rating is None or artifact.uncertainty is None):
            raise SportMemoryError("supported sport memory requires rating and uncertainty")
        if state == "INSUFFICIENT" and (artifact.rating is not None or artifact.uncertainty is not None):
            raise SportMemoryError("insufficient sport memory cannot carry rating or uncertainty")
        if artifact.last_observed_at is None:
            if artifact.age_seconds is not None:
                raise SportMemoryError("age_seconds requires last_observed_at")
            if any((support, effective_sample, opponent_count)):
                raise SportMemoryError("memory without observation cannot carry support")
        else:
            observed = _instant("last_observed_at", artifact.last_observed_at)
            if observed > cutoff:
                raise SportMemoryError("last_observed_at cannot exceed causal cutoff")
            if support == 0:
                raise SportMemoryError("last_observed_at requires positive support")
            age_seconds = _nonnegative_int("age_seconds", artifact.age_seconds)
            expected_age_seconds = max(0, int((cutoff - observed).total_seconds()))
            if age_seconds != expected_age_seconds:
                raise SportMemoryError("age_seconds does not match causal cutoff")
        _sha256("authority_generation_sha256", artifact.authority_generation_sha256)

    @staticmethod
    def _validate_consumption(
        record: DecisionMemoryConsumption,
        artifact: SportMemoryArtifact,
    ) -> None:
        if type(record) is not DecisionMemoryConsumption:
            raise SportMemoryError("invalid sport memory consumption")
        _sha256("consumption_id", record.consumption_id)
        _text("decision_id", record.decision_id)
        if record.memory_id != artifact.memory_id:
            raise SportMemoryError("consumption references wrong sport memory artifact")
        cutoff = _instant("decision_cutoff", record.decision_cutoff)
        consumed = _instant("consumed_at", record.consumed_at)
        if _instant("causal_cutoff", artifact.causal_cutoff) > cutoff:
            raise SportMemoryError("future causal snapshot cannot be consumed by decision")
        if _instant("published_at", artifact.published_at) > cutoff:
            raise SportMemoryError("snapshot unavailable at decision cutoff")
        if consumed < cutoff:
            raise SportMemoryError("consumption cannot precede decision cutoff")

    def get(self, memory_id: str) -> SportMemoryArtifact:
        key = _sha256("memory_id", memory_id)
        try:
            return self._artifacts[key]
        except KeyError as exc:
            raise SportMemoryError("unknown sport memory artifact") from exc

    def participant_history(
        self,
        participant_entity_id: str,
        scope: SportMemoryScope,
        *,
        view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
    ) -> tuple[SportMemoryArtifact, ...]:
        participant = _text("participant_entity_id", participant_entity_id)
        requested_view = _require_identity_view(view)
        if not isinstance(scope, SportMemoryScope):
            raise TypeError("scope must be SportMemoryScope")
        records = (
            artifact
            for artifact in self._artifacts.values()
            if artifact.participant_entity_id == participant
            and artifact.scope == scope
            and artifact.identity_view is requested_view
        )
        return tuple(
            sorted(
                records,
                key=lambda artifact: (
                    _instant("causal_cutoff", artifact.causal_cutoff),
                    _instant("published_at", artifact.published_at),
                    artifact.memory_id,
                ),
            )
        )

    def last_causal_snapshot(
        self,
        participant_entity_id: str,
        scope: SportMemoryScope,
        *,
        as_of: str,
        view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
    ) -> SportMemoryArtifact | None:
        instant = _instant("as_of", as_of)
        requested_view = _require_identity_view(view)
        candidates = [
            artifact
            for artifact in self.participant_history(
                participant_entity_id,
                scope,
                view=requested_view,
            )
            if _instant("causal_cutoff", artifact.causal_cutoff) <= instant
            and _instant("published_at", artifact.published_at) <= instant
        ]
        return (
            max(
                candidates,
                key=lambda artifact: (
                    _instant("causal_cutoff", artifact.causal_cutoff),
                    _instant("published_at", artifact.published_at),
                    artifact.memory_id,
                ),
            )
            if candidates
            else None
        )

    def matchup_as_of(
        self,
        subject_participant_entity_id: str,
        opponent_participant_entity_id: str,
        scope: SportMemoryScope,
        *,
        as_of: str,
        view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
    ) -> SportMemoryMatchupEvidence:
        """Select exact supported immutable memories visible at decision time.

        The checkpoint-bound runtime is required for product-positive evidence.
        Durable use is completed by record_matchup_consumption before a decision
        is treated as published.
        """
        self._require_durable_positive_authority()
        subject_id = _text(
            "subject_participant_entity_id", subject_participant_entity_id
        )
        opponent_id = _text(
            "opponent_participant_entity_id", opponent_participant_entity_id
        )
        if subject_id == opponent_id:
            raise SportMemoryError("matchup participants must be distinct")
        if not isinstance(scope, SportMemoryScope):
            raise TypeError("scope must be SportMemoryScope")
        requested_view = _require_identity_view(view)
        if requested_view is not IdentityView.AS_KNOWN_AT_DECISION:
            raise SportMemoryError(
                "decision-time matchup memory requires AS_KNOWN_AT_DECISION view"
            )
        decision_time = _instant("as_of", as_of)
        subject = self.last_causal_snapshot(
            subject_id, scope, as_of=as_of, view=requested_view
        )
        opponent = self.last_causal_snapshot(
            opponent_id, scope, as_of=as_of, view=requested_view
        )
        if subject is None or opponent is None:
            raise SportMemoryError(
                "decision-time matchup requires both causal participant memories"
            )
        for label, artifact in (("subject", subject), ("opponent", opponent)):
            if artifact.state != "SUPPORTED":
                raise SportMemoryError(
                    f"decision-time {label} memory is not supported"
                )
            if artifact.rating is None or artifact.uncertainty is None:
                raise SportMemoryError(
                    f"decision-time {label} memory lacks rating/uncertainty"
                )
            if artifact.age_seconds is None or artifact.last_observed_at is None:
                raise SportMemoryError(
                    f"decision-time {label} memory lacks staleness evidence"
                )
            if (
                artifact.authority_generation_sha256
                != self.authority_generation_sha256
            ):
                raise SportMemoryError(
                    "matchup memory authority generation does not match runtime"
                )
            if _instant("published_at", artifact.published_at) > decision_time:
                raise SportMemoryError(
                    "future memory publication cannot enter matchup"
                )

        causal_cutoff = max(
            (subject.causal_cutoff, opponent.causal_cutoff),
            key=lambda value: _instant("causal_cutoff", value),
        )
        published_at = max(
            (subject.published_at, opponent.published_at),
            key=lambda value: _instant("published_at", value),
        )
        candidate = SportMemoryMatchupEvidence(
            matchup_id="0" * 64,
            subject_memory_id=subject.memory_id,
            opponent_memory_id=opponent.memory_id,
            subject_participant_entity_id=subject.participant_entity_id,
            opponent_participant_entity_id=opponent.participant_entity_id,
            scope=scope,
            identity_view=requested_view,
            as_of=_time_text_from_instant(decision_time),
            causal_cutoff=causal_cutoff,
            published_at=published_at,
            subject_support=subject.support,
            opponent_support=opponent.support,
            subject_uncertainty=subject.uncertainty,
            opponent_uncertainty=opponent.uncertainty,
            subject_age_seconds=max(
                0,
                int(
                    (
                        decision_time
                        - _instant(
                            "subject last_observed_at",
                            subject.last_observed_at,
                        )
                    ).total_seconds()
                ),
            ),
            opponent_age_seconds=max(
                0,
                int(
                    (
                        decision_time
                        - _instant(
                            "opponent last_observed_at",
                            opponent.last_observed_at,
                        )
                    ).total_seconds()
                ),
            ),
            authority_generation_sha256=self.authority_generation_sha256,
        )
        return replace(
            candidate,
            matchup_id=_digest(candidate.payload(include_id=False)),
        )

    def verify_matchup_evidence(
        self,
        matchup: SportMemoryMatchupEvidence,
    ) -> SportMemoryMatchupEvidence:
        """Re-resolve and verify a matchup against canonical latest-as-of memory."""
        self._require_durable_positive_authority()
        if type(matchup) is not SportMemoryMatchupEvidence:
            raise TypeError("matchup must be SportMemoryMatchupEvidence")
        if _digest(matchup.payload(include_id=False)) != matchup.matchup_id:
            raise SportMemoryError("sport-memory matchup digest mismatch")
        canonical = self.matchup_as_of(
            matchup.subject_participant_entity_id,
            matchup.opponent_participant_entity_id,
            matchup.scope,
            as_of=matchup.as_of,
            view=matchup.identity_view,
        )
        if canonical != matchup:
            raise SportMemoryError(
                "sport-memory matchup is not canonical latest-as-of evidence"
            )
        return canonical

    def record_matchup_consumption(
        self,
        *,
        decision_id: str,
        matchup: SportMemoryMatchupEvidence,
        decision_cutoff: str,
        consumed_at: str,
    ) -> tuple[DecisionMemoryConsumption, DecisionMemoryConsumption]:
        """Durably publish both exact participant bindings as one checkpoint write.

        No checkpoint containing only one member is emitted by this path. If a
        predecessor head crashed after its first member write, the surviving
        durable member supplies the canonical consumed_at on retry so the missing
        counterpart can be recovered without relabelling the existing witness.
        """
        if type(matchup) is not SportMemoryMatchupEvidence:
            raise TypeError("matchup must be SportMemoryMatchupEvidence")
        if (
            matchup.authority_generation_sha256
            != self.authority_generation_sha256
        ):
            raise SportMemoryError("matchup authority generation changed")
        decision = _text("decision_id", decision_id)
        cutoff = _text("decision_cutoff", decision_cutoff)
        requested_consumed = _text("consumed_at", consumed_at)
        cutoff_instant = _instant("decision_cutoff", cutoff)
        if _instant("consumed_at", requested_consumed) < cutoff_instant:
            raise SportMemoryError("consumption cannot precede decision cutoff")
        if _instant("matchup as_of", matchup.as_of) != cutoff_instant:
            raise SportMemoryError(
                "matchup as_of must equal decision cutoff"
            )
        self.verify_matchup_evidence(matchup)

        artifacts = (
            self.get(matchup.subject_memory_id),
            self.get(matchup.opponent_memory_id),
        )
        for artifact in artifacts:
            if artifact.scope != matchup.scope:
                raise SportMemoryError(
                    "sport memory scope mismatch; refusing cross-domain reuse"
                )
            if artifact.identity_view is not matchup.identity_view:
                raise SportMemoryError(
                    "sport memory identity view mismatch; refusing causal-view reuse"
                )

        existing_records: list[DecisionMemoryConsumption] = []
        for artifact in artifacts:
            existing_id = self._decision_consumptions.get(
                (decision, artifact.memory_id)
            )
            if existing_id is not None:
                existing_records.append(self._consumptions[existing_id])

        canonical_consumed = requested_consumed
        if existing_records:
            if any(record.decision_cutoff != cutoff for record in existing_records):
                raise SportMemoryError("decision consumption semantic drift")
            canonical_consumed = existing_records[0].consumed_at
            if any(
                record.consumed_at != canonical_consumed
                for record in existing_records[1:]
            ):
                raise SportMemoryError("matchup consumption timestamp drift")

        resolved: dict[str, DecisionMemoryConsumption] = {}
        staged: list[tuple[tuple[str, str], DecisionMemoryConsumption]] = []
        for artifact in artifacts:
            payload = {
                "decision_id": decision,
                "memory_id": artifact.memory_id,
                "decision_cutoff": cutoff,
                "consumed_at": canonical_consumed,
            }
            record = DecisionMemoryConsumption(
                consumption_id=_digest(payload),
                **payload,
            )
            self._validate_consumption(record, artifact)
            key = (decision, artifact.memory_id)
            existing_id = self._decision_consumptions.get(key)
            if existing_id is not None:
                existing = self._consumptions[existing_id]
                if (
                    existing.decision_cutoff,
                    existing.consumed_at,
                ) != (cutoff, canonical_consumed):
                    raise SportMemoryError("decision consumption semantic drift")
                resolved[artifact.memory_id] = existing
                continue
            self._assert_no_participant_rebind(
                decision_id=decision,
                artifact=artifact,
            )
            staged.append((key, record))
            resolved[artifact.memory_id] = record

        for key, record in staged:
            self._consumptions[record.consumption_id] = record
            self._decision_consumptions[key] = record.consumption_id
        try:
            if staged:
                self._persist()
        except Exception:
            for key, record in staged:
                self._decision_consumptions.pop(key, None)
                self._consumptions.pop(record.consumption_id, None)
            raise

        return (
            resolved[matchup.subject_memory_id],
            resolved[matchup.opponent_memory_id],
        )

    def _assert_no_participant_rebind(
        self,
        *,
        decision_id: str,
        artifact: SportMemoryArtifact,
    ) -> None:
        for (
            existing_decision,
            existing_memory_id,
        ), _ in self._decision_consumptions.items():
            if (
                existing_decision != decision_id
                or existing_memory_id == artifact.memory_id
            ):
                continue
            existing_artifact = self._artifacts[existing_memory_id]
            if (
                existing_artifact.participant_entity_id
                == artifact.participant_entity_id
                and existing_artifact.scope == artifact.scope
                and existing_artifact.identity_view is artifact.identity_view
            ):
                raise SportMemoryError(
                    "decision participant memory semantic drift"
                )

    def record_consumption(
        self,
        *,
        decision_id: str,
        memory_id: str,
        decision_cutoff: str,
        consumed_at: str,
        expected_scope: SportMemoryScope,
        expected_view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
    ) -> DecisionMemoryConsumption:
        decision = _text("decision_id", decision_id)
        artifact = self.get(memory_id)
        requested_view = _require_identity_view(expected_view, name="expected_view")
        if not isinstance(expected_scope, SportMemoryScope):
            raise TypeError("expected_scope must be SportMemoryScope")
        if artifact.scope != expected_scope:
            raise SportMemoryError("sport memory scope mismatch; refusing cross-domain reuse")
        if artifact.identity_view is not requested_view:
            raise SportMemoryError(
                "sport memory identity view mismatch; refusing causal-view reuse"
            )
        cutoff = _text("decision_cutoff", decision_cutoff)
        consumed = _text("consumed_at", consumed_at)
        payload = {
            "decision_id": decision,
            "memory_id": artifact.memory_id,
            "decision_cutoff": cutoff,
            "consumed_at": consumed,
        }
        record = DecisionMemoryConsumption(consumption_id=_digest(payload), **payload)
        self._validate_consumption(record, artifact)
        key = (decision, artifact.memory_id)
        existing_id = self._decision_consumptions.get(key)
        if existing_id is not None:
            existing = self._consumptions[existing_id]
            if (existing.decision_cutoff, existing.consumed_at) != (
                cutoff,
                consumed,
            ):
                raise SportMemoryError("decision consumption semantic drift")
            return existing
        self._assert_no_participant_rebind(
            decision_id=decision,
            artifact=artifact,
        )
        self._consumptions[record.consumption_id] = record
        self._decision_consumptions[key] = record.consumption_id
        try:
            self._persist()
        except Exception:
            self._decision_consumptions.pop(key, None)
            self._consumptions.pop(record.consumption_id, None)
            raise
        return record

    def consumptions_for_artifact(self, memory_id: str) -> tuple[DecisionMemoryConsumption, ...]:
        artifact = self.get(memory_id)
        records = (r for r in self._consumptions.values() if r.memory_id == artifact.memory_id)
        return tuple(sorted(records, key=lambda r: (_instant("decision_cutoff", r.decision_cutoff), r.consumption_id)))

    def consumptions_for_decision(
        self, decision_id: str
    ) -> tuple[DecisionMemoryConsumption, ...]:
        decision = _text("decision_id", decision_id)
        records = (
            record
            for record in self._consumptions.values()
            if record.decision_id == decision
        )
        return tuple(
            sorted(
                records,
                key=lambda record: (
                    self._artifacts[record.memory_id].participant_entity_id,
                    self._artifacts[record.memory_id].scope.sport_id,
                    self._artifacts[record.memory_id].scope.league_entity_id,
                    self._artifacts[record.memory_id].scope.market_context_id,
                    record.memory_id,
                ),
            )
        )

    def _persist(self) -> None:
        atomic_write_json(self.path, {
            "schema_version": self.SCHEMA_VERSION,
            "authority_generation_sha256": self.authority_generation_sha256,
            "artifacts": [a.payload() for a in sorted(self._artifacts.values(), key=lambda x: x.memory_id)],
            "consumptions": [r.payload() for r in sorted(self._consumptions.values(), key=lambda x: x.consumption_id)],
        })

    def _load(self) -> None:
        try:
            raw_payload = json.loads(self.path.read_text(encoding="utf-8"))
            payload = _require_exact_keys("sport memory checkpoint", raw_payload, self._CHECKPOINT_KEYS)
            if type(payload["schema_version"]) is not int or payload["schema_version"] != self.SCHEMA_VERSION:
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
                artifact = self._artifacts.get(record.memory_id)
                if artifact is None:
                    raise SportMemoryError("consumption references unknown memory artifact")
                self._validate_consumption(record, artifact)
                if record.consumption_id in self._consumptions:
                    raise SportMemoryError("duplicate sport memory consumption")
                key = (record.decision_id, record.memory_id)
                if key in self._decision_consumptions:
                    raise SportMemoryError(
                        "duplicate decision/memory consumption"
                    )
                self._assert_no_participant_rebind(
                    decision_id=record.decision_id,
                    artifact=artifact,
                )
                self._consumptions[record.consumption_id] = record
                self._decision_consumptions[key] = record.consumption_id
        except SportMemoryError:
            raise
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, OverflowError) as exc:
            raise SportMemoryError("invalid sport memory checkpoint") from exc

    @classmethod
    def _artifact_from_raw(cls, raw: object, generation: str) -> SportMemoryArtifact:
        r = _require_exact_keys("sport memory artifact", raw, cls._ARTIFACT_KEYS)
        scope_raw = r["scope"]
        if type(scope_raw) is not dict:
            raise SportMemoryError("invalid sport memory scope")
        input_ids = r["input_performance_ids"]
        if type(input_ids) is not list:
            raise SportMemoryError("input_performance_ids must be a list")
        artifact = SportMemoryArtifact(
            memory_id=_sha256("memory_id", r["memory_id"]),
            participant_entity_id=_text("participant_entity_id", r["participant_entity_id"]),
            scope=SportMemoryScope(**scope_raw),
            identity_view=_identity_view_from_raw(r["identity_view"]),
            causal_cutoff=_text("causal_cutoff", r["causal_cutoff"]),
            published_at=_text("published_at", r["published_at"]),
            rating_snapshot_id=_sha256("rating_snapshot_id", r["rating_snapshot_id"]),
            feature_snapshot_id=_sha256("feature_snapshot_id", r["feature_snapshot_id"]),
            input_digest=_sha256("input_digest", r["input_digest"]),
            input_performance_ids=tuple(_sha256("input_performance_id", v) for v in input_ids),
            support=_nonnegative_int("support", r["support"]),
            effective_sample=_nonnegative_int("effective_sample", r["effective_sample"]),
            opponent_count=_nonnegative_int("opponent_count", r["opponent_count"]),
            rating=_optional_text("rating", r["rating"]),
            uncertainty=_optional_text("uncertainty", r["uncertainty"]),
            state=_text("state", r["state"]),
            last_observed_at=_optional_text("last_observed_at", r["last_observed_at"]),
            age_seconds=(
                None
                if r["age_seconds"] is None
                else _nonnegative_int("age_seconds", r["age_seconds"])
            ),
            authority_generation_sha256=_sha256(
                "authority_generation_sha256", r["authority_generation_sha256"]
            ),
        )
        if artifact.authority_generation_sha256 != generation:
            raise SportMemoryError("artifact authority generation mismatch")
        cls._validate_artifact(artifact)
        if _digest(artifact.payload(include_id=False)) != artifact.memory_id:
            raise SportMemoryError("sport memory artifact digest mismatch")
        return artifact

    @classmethod
    def _consumption_from_raw(cls, raw: object) -> DecisionMemoryConsumption:
        r = _require_exact_keys("sport memory consumption", raw, cls._CONSUMPTION_KEYS)
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
