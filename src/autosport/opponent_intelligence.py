"""Causal opponent-performance graph and immutable rating/feature snapshots.

Consumes canonical participant identity evidence only.  This module is not an
identity registry, predictor, promotion gate, risk engine, or execution path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .integrity import atomic_write_json
from .participant_identity import (
    EntityKind,
    IdentityView,
    ParticipantIdentityRegistry,
)

_SCHEMA = "autosport.opponent_intelligence"
_VERSION = 1


class OpponentIntelligenceError(ValueError):
    """Raised when causal opponent/rating evidence is malformed or ambiguous."""


class SnapshotState(StrEnum):
    SUPPORTED = "SUPPORTED"
    INSUFFICIENT = "INSUFFICIENT"


class RecomputeStatus(StrEnum):
    REQUIRED = "REQUIRED"
    RESOLVED = "RESOLVED"


class InvalidationTarget(StrEnum):
    OPPONENT_EDGE = "OPPONENT_EDGE"
    RATING_SNAPSHOT = "RATING_SNAPSHOT"
    FEATURE_SNAPSHOT = "FEATURE_SNAPSHOT"


class InvalidationReason(StrEnum):
    OUTCOME_CORRECTION = "OUTCOME_CORRECTION"
    IDENTITY_CORRECTION = "IDENTITY_CORRECTION"


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise OpponentIntelligenceError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _instant(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OpponentIntelligenceError(f"{name} must be ISO-8601") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise OpponentIntelligenceError(f"{name} must include a timezone")
    return result.astimezone(timezone.utc)


def _time_text(name: str, value: object) -> str:
    return _instant(name, value).isoformat().replace("+00:00", "Z")


def _sha256(name: str, value: object) -> str:
    text = _text(name, value).lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise OpponentIntelligenceError(f"{name} must be SHA-256 hex")
    return text


def _decimal(name: str, value: object) -> Decimal:
    if isinstance(value, bool):
        raise OpponentIntelligenceError(f"{name} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise OpponentIntelligenceError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise OpponentIntelligenceError(f"{name} must be a finite decimal")
    return result


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise OpponentIntelligenceError("decimal value must be finite")
    return format(value.normalize(), "f")


def _digest(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ObservedPerformance:
    event_id: str
    source_id: str
    subject_alias: str
    opponent_alias: str
    sport_id: str
    league_alias: str
    market_context_id: str
    score: str
    observed_at: str
    available_at: str
    recorded_at: str
    evidence_sha256: str
    supersedes_performance_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "source_id",
            "subject_alias",
            "opponent_alias",
            "sport_id",
            "league_alias",
            "market_context_id",
        ):
            _text(name, getattr(self, name))
        score = _decimal("score", self.score)
        if score < 0 or score > 1:
            raise OpponentIntelligenceError("score must be between 0 and 1")
        observed = _instant("observed_at", self.observed_at)
        available = _instant("available_at", self.available_at)
        recorded = _instant("recorded_at", self.recorded_at)
        if available < observed:
            raise OpponentIntelligenceError(
                "performance cannot be available before observed_at"
            )
        if recorded < available:
            raise OpponentIntelligenceError(
                "performance cannot be recorded before available_at"
            )
        _sha256("evidence_sha256", self.evidence_sha256)
        if self.supersedes_performance_id is not None:
            _sha256("supersedes_performance_id", self.supersedes_performance_id)

    def payload(self) -> dict[str, str | None]:
        return {
            "event_id": self.event_id,
            "source_id": self.source_id,
            "subject_alias": self.subject_alias,
            "opponent_alias": self.opponent_alias,
            "sport_id": self.sport_id,
            "league_alias": self.league_alias,
            "market_context_id": self.market_context_id,
            "score": _decimal_text(_decimal("score", self.score)),
            "observed_at": _time_text("observed_at", self.observed_at),
            "available_at": _time_text("available_at", self.available_at),
            "recorded_at": _time_text("recorded_at", self.recorded_at),
            "evidence_sha256": _sha256(
                "evidence_sha256", self.evidence_sha256
            ),
            "supersedes_performance_id": self.supersedes_performance_id,
        }

    @property
    def performance_id(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True, slots=True)
class PerformanceRecord:
    performance_id: str
    observation: ObservedPerformance
    subject_entity_id: str
    opponent_entity_id: str
    league_entity_id: str
    subject_alias_record_id: str
    opponent_alias_record_id: str
    league_alias_record_id: str

    def __post_init__(self) -> None:
        if self.performance_id != self.observation.performance_id:
            raise OpponentIntelligenceError(
                "performance_id does not match immutable observation"
            )
        for name in ("subject_entity_id", "opponent_entity_id", "league_entity_id"):
            _text(name, getattr(self, name))
        _sha256("subject_alias_record_id", self.subject_alias_record_id)
        _sha256("opponent_alias_record_id", self.opponent_alias_record_id)
        _sha256("league_alias_record_id", self.league_alias_record_id)
        if self.subject_entity_id == self.opponent_entity_id:
            raise OpponentIntelligenceError(
                "subject and opponent must be distinct canonical identities"
            )

    def payload(self) -> dict[str, Any]:
        return {
            "performance_id": self.performance_id,
            "observation": self.observation.payload(),
            "subject_entity_id": self.subject_entity_id,
            "opponent_entity_id": self.opponent_entity_id,
            "league_entity_id": self.league_entity_id,
            "subject_alias_record_id": self.subject_alias_record_id,
            "opponent_alias_record_id": self.opponent_alias_record_id,
            "league_alias_record_id": self.league_alias_record_id,
        }


@dataclass(frozen=True, slots=True)
class OpponentEdge:
    performance_id: str
    event_id: str
    subject_entity_id: str
    opponent_entity_id: str
    sport_id: str
    league_entity_id: str
    market_context_id: str
    score: str
    observed_at: str
    available_at: str


@dataclass(frozen=True, slots=True)
class RatingSnapshot:
    snapshot_id: str
    participant_entity_id: str
    sport_id: str
    league_id: str
    market_context_id: str
    view: IdentityView
    causal_cutoff: str
    published_at: str
    algorithm_family: str
    algorithm_version: str
    config_sha256: str
    code_sha256: str
    dependency_sha256: str
    input_performance_ids: tuple[str, ...]
    input_digest: str
    support: int
    opponent_count: int
    rating: str | None
    uncertainty: str | None
    state: SnapshotState

    def payload(self, *, include_id: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "participant_entity_id": self.participant_entity_id,
            "sport_id": self.sport_id,
            "league_id": self.league_id,
            "market_context_id": self.market_context_id,
            "view": self.view.value,
            "causal_cutoff": self.causal_cutoff,
            "published_at": self.published_at,
            "algorithm_family": self.algorithm_family,
            "algorithm_version": self.algorithm_version,
            "config_sha256": self.config_sha256,
            "code_sha256": self.code_sha256,
            "dependency_sha256": self.dependency_sha256,
            "input_performance_ids": list(self.input_performance_ids),
            "input_digest": self.input_digest,
            "support": self.support,
            "opponent_count": self.opponent_count,
            "rating": self.rating,
            "uncertainty": self.uncertainty,
            "state": self.state.value,
        }
        if include_id:
            result["snapshot_id"] = self.snapshot_id
        return result


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    snapshot_id: str
    rating_snapshot_id: str
    participant_entity_id: str
    sport_id: str
    league_id: str
    market_context_id: str
    view: IdentityView
    causal_cutoff: str
    published_at: str
    input_digest: str
    support: int
    opponent_count: int
    last_observed_at: str | None
    age_seconds: int | None
    state: SnapshotState

    def payload(self, *, include_id: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "rating_snapshot_id": self.rating_snapshot_id,
            "participant_entity_id": self.participant_entity_id,
            "sport_id": self.sport_id,
            "league_id": self.league_id,
            "market_context_id": self.market_context_id,
            "view": self.view.value,
            "causal_cutoff": self.causal_cutoff,
            "published_at": self.published_at,
            "input_digest": self.input_digest,
            "support": self.support,
            "opponent_count": self.opponent_count,
            "last_observed_at": self.last_observed_at,
            "age_seconds": self.age_seconds,
            "state": self.state.value,
        }
        if include_id:
            result["snapshot_id"] = self.snapshot_id
        return result


@dataclass(frozen=True, slots=True)
class DownstreamInvalidation:
    invalidation_id: str
    target_kind: InvalidationTarget
    target_id: str
    reason: InvalidationReason
    evidence_id: str
    detected_at: str
    recompute_status: RecomputeStatus = RecomputeStatus.REQUIRED

    def payload(self, *, include_id: bool = True) -> dict[str, str]:
        result = {
            "target_kind": self.target_kind.value,
            "target_id": self.target_id,
            "reason": self.reason.value,
            "evidence_id": self.evidence_id,
            "detected_at": self.detected_at,
            "recompute_status": self.recompute_status.value,
        }
        if include_id:
            result["invalidation_id"] = self.invalidation_id
        return result


class OpponentIntelligenceStore:
    """Restart-safe causal performance evidence and immutable snapshots."""

    def __init__(
        self, path: Path, identity_registry: ParticipantIdentityRegistry
    ):
        if not isinstance(identity_registry, ParticipantIdentityRegistry):
            raise TypeError(
                "identity_registry must be ParticipantIdentityRegistry"
            )
        self.path = Path(path)
        self.identity_registry = identity_registry
        self._performances: dict[str, PerformanceRecord] = {}
        self._ratings: dict[str, RatingSnapshot] = {}
        self._features: dict[str, FeatureSnapshot] = {}
        self._invalidations: dict[str, DownstreamInvalidation] = {}
        if self.path.exists():
            self._load()

    @classmethod
    def initialize_pristine(
        cls,
        path: Path,
        identity_registry: ParticipantIdentityRegistry,
    ) -> "OpponentIntelligenceStore":
        store = cls(path, identity_registry)
        if store.path.exists():
            raise OpponentIntelligenceError(
                "opponent intelligence store already exists"
            )
        store._persist()
        return store

    def record_performance(
        self,
        observation: ObservedPerformance,
        *,
        identity_view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
    ) -> PerformanceRecord:
        if not isinstance(observation, ObservedPerformance):
            raise TypeError("observation must be ObservedPerformance")
        if not isinstance(identity_view, IdentityView):
            raise TypeError("identity_view must be IdentityView")

        performance_id = observation.performance_id
        existing = self._performances.get(performance_id)
        if existing is not None:
            return existing

        subject_alias = self.identity_registry.resolve_alias_record(
            observation.source_id,
            observation.subject_alias,
            as_of=observation.available_at,
            view=identity_view,
        )
        opponent_alias = self.identity_registry.resolve_alias_record(
            observation.source_id,
            observation.opponent_alias,
            as_of=observation.available_at,
            view=identity_view,
        )
        league_alias = self.identity_registry.resolve_alias_record(
            observation.source_id,
            observation.league_alias,
            as_of=observation.available_at,
            view=identity_view,
        )
        subject_entity = self.identity_registry.resolve_alias(
            observation.source_id,
            observation.subject_alias,
            as_of=observation.available_at,
            view=identity_view,
        )
        opponent_entity = self.identity_registry.resolve_alias(
            observation.source_id,
            observation.opponent_alias,
            as_of=observation.available_at,
            view=identity_view,
        )
        league_entity = self.identity_registry.resolve_alias(
            observation.source_id,
            observation.league_alias,
            as_of=observation.available_at,
            view=identity_view,
        )
        if (
            subject_entity.entity_id != subject_alias.entity_id
            or opponent_entity.entity_id != opponent_alias.entity_id
            or league_entity.entity_id != league_alias.entity_id
        ):
            raise OpponentIntelligenceError(
                "identity authority returned inconsistent alias/entity evidence"
            )
        if subject_entity.entity_id == opponent_entity.entity_id:
            raise OpponentIntelligenceError(
                "subject and opponent resolve to the same canonical identity"
            )
        if (
            subject_entity.kind is not opponent_entity.kind
            or subject_entity.kind
            not in (EntityKind.PARTICIPANT, EntityKind.TEAM)
        ):
            raise OpponentIntelligenceError(
                "opponent performance requires same-kind PARTICIPANT or TEAM identities"
            )
        if league_entity.kind is not EntityKind.LEAGUE:
            raise OpponentIntelligenceError(
                "league context must resolve through canonical LEAGUE identity"
            )

        supersedes = observation.supersedes_performance_id
        if supersedes is not None:
            predecessor = self._performances.get(supersedes)
            if predecessor is None:
                raise OpponentIntelligenceError(
                    "performance correction references unknown predecessor"
                )
            if any(
                item.observation.supersedes_performance_id == supersedes
                for item in self._performances.values()
            ):
                raise OpponentIntelligenceError(
                    "performance correction fork is not allowed"
                )
            before = predecessor.observation
            for name in ("event_id", "source_id", "sport_id", "league_alias", "market_context_id"):
                if getattr(before, name) != getattr(observation, name):
                    raise OpponentIntelligenceError(
                        "performance correction must preserve event/context identity"
                    )
            if _instant(
                "available_at", observation.available_at
            ) <= _instant("predecessor available_at", before.available_at):
                raise OpponentIntelligenceError(
                    "performance correction must become available later"
                )
            if _instant(
                "recorded_at", observation.recorded_at
            ) <= _instant("predecessor recorded_at", before.recorded_at):
                raise OpponentIntelligenceError(
                    "performance correction must be recorded later"
                )

        record = PerformanceRecord(
            performance_id=performance_id,
            observation=observation,
            subject_entity_id=subject_entity.entity_id,
            opponent_entity_id=opponent_entity.entity_id,
            league_entity_id=league_entity.entity_id,
            subject_alias_record_id=subject_alias.record_id,
            opponent_alias_record_id=opponent_alias.record_id,
            league_alias_record_id=league_alias.record_id,
        )
        candidate_performances = dict(self._performances)
        candidate_performances[performance_id] = record
        candidate_invalidations = dict(self._invalidations)
        if supersedes is not None:
            predecessor = self._performances[supersedes]
            identity_changed = (
                predecessor.subject_entity_id != record.subject_entity_id
                or predecessor.opponent_entity_id != record.opponent_entity_id
                or predecessor.league_entity_id != record.league_entity_id
            )
            score_changed = (
                _decimal("predecessor score", predecessor.observation.score)
                != _decimal("correction score", observation.score)
            )
            reason = (
                InvalidationReason.IDENTITY_CORRECTION
                if identity_changed and not score_changed
                else InvalidationReason.OUTCOME_CORRECTION
            )
            self._invalidate_targets_for_performance(
                supersedes,
                reason=reason,
                evidence_id=performance_id,
                detected_at=observation.recorded_at,
                target=candidate_invalidations,
            )
        self._persist_state(
            performances=candidate_performances,
            invalidations=candidate_invalidations,
        )
        self._performances = candidate_performances
        self._invalidations = candidate_invalidations
        return record

    def graph_edges(
        self,
        *,
        as_of: str,
        view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
        sport_id: str | None = None,
        league_entity_id: str | None = None,
        market_context_id: str | None = None,
    ) -> tuple[OpponentEdge, ...]:
        records = self._active_performances(as_of=as_of, view=view)
        edges: list[OpponentEdge] = []
        for record in records:
            observation = record.observation
            if sport_id is not None and observation.sport_id != _text(
                "sport_id", sport_id
            ):
                continue
            if (
                league_entity_id is not None
                and record.league_entity_id != _text(
                    "league_entity_id", league_entity_id
                )
            ):
                continue
            if (
                market_context_id is not None
                and observation.market_context_id != _text(
                    "market_context_id", market_context_id
                )
            ):
                continue
            edges.append(
                OpponentEdge(
                    performance_id=record.performance_id,
                    event_id=observation.event_id,
                    subject_entity_id=record.subject_entity_id,
                    opponent_entity_id=record.opponent_entity_id,
                    sport_id=observation.sport_id,
                    league_entity_id=record.league_entity_id,
                    market_context_id=observation.market_context_id,
                    score=_decimal_text(
                        _decimal("score", observation.score)
                    ),
                    observed_at=_time_text(
                        "observed_at", observation.observed_at
                    ),
                    available_at=_time_text(
                        "available_at", observation.available_at
                    ),
                )
            )
        return tuple(edges)

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
    ) -> tuple[RatingSnapshot, FeatureSnapshot]:
        if not isinstance(view, IdentityView):
            raise TypeError("view must be IdentityView")
        participant = _text(
            "participant_entity_id", participant_entity_id
        )
        sport = _text("sport_id", sport_id)
        league = _text("league_entity_id", league_entity_id)
        market = _text("market_context_id", market_context_id)
        code = _sha256("code_sha256", code_sha256)
        dependency = _sha256("dependency_sha256", dependency_sha256)
        cutoff = _time_text("causal_cutoff", causal_cutoff)
        publication = _time_text("published_at", published_at)
        if _instant("published_at", publication) < _instant(
            "causal_cutoff", cutoff
        ):
            raise OpponentIntelligenceError(
                "snapshot cannot be published before causal cutoff"
            )
        if type(min_support) is not int or min_support < 1:
            raise OpponentIntelligenceError(
                "min_support must be a positive integer"
            )
        if type(max_age_seconds) is not int or max_age_seconds < 1:
            raise OpponentIntelligenceError(
                "max_age_seconds must be a positive integer"
            )
        version = _text("algorithm_version", algorithm_version)
        family = "bounded-mean-score"
        config_sha256 = _digest(
            {
                "algorithm_family": family,
                "algorithm_version": version,
                "min_support": min_support,
                "max_age_seconds": max_age_seconds,
                "code_sha256": code,
                "dependency_sha256": dependency,
                "numeric": "decimal",
                "score_domain": "[0,1]",
            }
        )
        known_entities = {
            entity_id
            for record in self._performances.values()
            for entity_id in (
                record.subject_entity_id,
                record.opponent_entity_id,
            )
        }
        if participant not in known_entities:
            raise OpponentIntelligenceError(
                "participant_entity_id has no canonical performance identity evidence"
            )

        inputs = [
            record
            for record in self._active_performances(
                as_of=cutoff, view=view
            )
            if record.observation.sport_id == sport
            and record.league_entity_id == league
            and record.observation.market_context_id == market
            and participant
            in (record.subject_entity_id, record.opponent_entity_id)
        ]
        inputs.sort(
            key=lambda item: (
                _instant(
                    "observed_at", item.observation.observed_at
                ),
                item.performance_id,
            )
        )
        input_ids = tuple(item.performance_id for item in inputs)
        input_digest = _digest(list(input_ids))
        scores: list[Decimal] = []
        opponents: set[str] = set()
        for item in inputs:
            score = _decimal("score", item.observation.score)
            if item.subject_entity_id == participant:
                scores.append(score)
                opponents.add(item.opponent_entity_id)
            else:
                scores.append(Decimal(1) - score)
                opponents.add(item.subject_entity_id)

        support = len(scores)
        last_observed_at = (
            max(
                (
                    _time_text(
                        "observed_at", item.observation.observed_at
                    )
                    for item in inputs
                ),
                key=lambda value: _instant(
                    "observed_at", value
                ),
            )
            if inputs
            else None
        )
        age_seconds = None
        if last_observed_at is not None:
            age_seconds = max(
                0,
                int(
                    (
                        _instant("causal_cutoff", cutoff)
                        - _instant(
                            "last_observed_at", last_observed_at
                        )
                    ).total_seconds()
                ),
            )
        fresh = (
            age_seconds is not None
            and age_seconds <= max_age_seconds
        )
        state = (
            SnapshotState.SUPPORTED
            if support >= min_support and fresh
            else SnapshotState.INSUFFICIENT
        )
        rating: str | None = None
        uncertainty: str | None = None
        if state is SnapshotState.SUPPORTED:
            with localcontext() as context:
                context.prec = 28
                mean = sum(scores, Decimal(0)) / Decimal(support)
                width = Decimal(1) / Decimal(support).sqrt()
            rating = _decimal_text(mean)
            uncertainty = _decimal_text(width)

        rating_payload = {
            "participant_entity_id": participant,
            "sport_id": sport,
            "league_id": league,
            "market_context_id": market,
            "view": view.value,
            "causal_cutoff": cutoff,
            "published_at": publication,
            "algorithm_family": family,
            "algorithm_version": version,
            "config_sha256": config_sha256,
            "code_sha256": code,
            "dependency_sha256": dependency,
            "input_performance_ids": list(input_ids),
            "input_digest": input_digest,
            "support": support,
            "opponent_count": len(opponents),
            "rating": rating,
            "uncertainty": uncertainty,
            "state": state.value,
        }
        rating_id = _digest(rating_payload)
        rating_snapshot = RatingSnapshot(
            rating_id,
            participant,
            sport,
            league,
            market,
            view,
            cutoff,
            publication,
            family,
            version,
            config_sha256,
            code,
            dependency,
            input_ids,
            input_digest,
            support,
            len(opponents),
            rating,
            uncertainty,
            state,
        )
        feature_payload = {
            "rating_snapshot_id": rating_id,
            "participant_entity_id": participant,
            "sport_id": sport,
            "league_id": league,
            "market_context_id": market,
            "view": view.value,
            "causal_cutoff": cutoff,
            "published_at": publication,
            "input_digest": input_digest,
            "support": support,
            "opponent_count": len(opponents),
            "last_observed_at": last_observed_at,
            "age_seconds": age_seconds,
            "state": state.value,
        }
        feature_id = _digest(feature_payload)
        feature_snapshot = FeatureSnapshot(
            feature_id,
            rating_id,
            participant,
            sport,
            league,
            market,
            view,
            cutoff,
            publication,
            input_digest,
            support,
            len(opponents),
            last_observed_at,
            age_seconds,
            state,
        )

        candidate_ratings = dict(self._ratings)
        candidate_features = dict(self._features)
        previous_rating = candidate_ratings.get(rating_id)
        previous_feature = candidate_features.get(feature_id)
        if (
            previous_rating is not None
            and previous_rating != rating_snapshot
        ):
            raise OpponentIntelligenceError(
                "conflicting immutable rating snapshot"
            )
        if (
            previous_feature is not None
            and previous_feature != feature_snapshot
        ):
            raise OpponentIntelligenceError(
                "conflicting immutable feature snapshot"
            )
        candidate_ratings[rating_id] = rating_snapshot
        candidate_features[feature_id] = feature_snapshot
        self._persist_state(
            ratings=candidate_ratings,
            features=candidate_features,
        )
        self._ratings = candidate_ratings
        self._features = candidate_features
        return rating_snapshot, feature_snapshot

    def refresh_identity_invalidations(
        self, *, detected_at: str
    ) -> tuple[DownstreamInvalidation, ...]:
        """Durably fence edges/snapshots affected by late identity truth."""
        detected = _time_text("detected_at", detected_at)
        candidate = dict(self._invalidations)
        for record in self._performances.values():
            evidence_ids: set[str] = set()
            observation = record.observation
            for alias, stored_record_id in (
                (
                    observation.subject_alias,
                    record.subject_alias_record_id,
                ),
                (
                    observation.opponent_alias,
                    record.opponent_alias_record_id,
                ),
                (
                    observation.league_alias,
                    record.league_alias_record_id,
                ),
            ):
                current = self.identity_registry.resolve_alias_record(
                    observation.source_id,
                    alias,
                    as_of=observation.available_at,
                    view=IdentityView.RESTATED_RESEARCH,
                )
                if current.record_id != stored_record_id:
                    evidence_ids.add(current.record_id)
            for entity_id in (
                record.subject_entity_id,
                record.opponent_entity_id,
                record.league_entity_id,
            ):
                for lineage in self.identity_registry.lineage_at(
                    entity_id,
                    as_of=detected,
                    view=IdentityView.RESTATED_RESEARCH,
                ):
                    if _instant(
                        "lineage available_at", lineage.available_at
                    ) > _instant(
                        "performance recorded_at",
                        observation.recorded_at,
                    ):
                        evidence_ids.add(lineage.record_id)
            for evidence_id in sorted(evidence_ids):
                self._invalidate_targets_for_performance(
                    record.performance_id,
                    reason=InvalidationReason.IDENTITY_CORRECTION,
                    evidence_id=evidence_id,
                    detected_at=detected,
                    target=candidate,
                )

        if candidate != self._invalidations:
            self._persist_state(invalidations=candidate)
            self._invalidations = candidate
        return self.invalidations()

    def invalidations(
        self, target_id: str | None = None
    ) -> tuple[DownstreamInvalidation, ...]:
        values: Iterable[DownstreamInvalidation] = (
            self._invalidations.values()
        )
        if target_id is not None:
            wanted = _sha256("target_id", target_id)
            values = (
                item
                for item in values
                if item.target_id == wanted
            )
        return tuple(
            sorted(
                values,
                key=lambda item: (
                    item.detected_at,
                    item.target_kind.value,
                    item.target_id,
                    item.invalidation_id,
                ),
            )
        )

    def _active_performances(
        self,
        *,
        as_of: str,
        view: IdentityView,
    ) -> tuple[PerformanceRecord, ...]:
        if not isinstance(view, IdentityView):
            raise TypeError("view must be IdentityView")
        moment = _instant("as_of", as_of)
        eligible: dict[str, PerformanceRecord] = {}
        for record in self._performances.values():
            observation = record.observation
            if _instant(
                "observed_at", observation.observed_at
            ) > moment:
                continue
            if view is IdentityView.AS_KNOWN_AT_DECISION and (
                _instant(
                    "available_at", observation.available_at
                ) > moment
                or _instant(
                    "recorded_at", observation.recorded_at
                ) > moment
            ):
                continue
            eligible[record.performance_id] = record

        superseded = {
            item.observation.supersedes_performance_id
            for item in eligible.values()
            if item.observation.supersedes_performance_id is not None
        }
        active = [
            item
            for key, item in eligible.items()
            if key not in superseded
        ]
        active.sort(
            key=lambda item: (
                _instant(
                    "observed_at", item.observation.observed_at
                ),
                item.performance_id,
            )
        )
        if view is IdentityView.RESTATED_RESEARCH:
            invalid_edge_ids = {
                item.target_id
                for item in self._invalidations.values()
                if item.target_kind
                is InvalidationTarget.OPPONENT_EDGE
                and item.reason
                is InvalidationReason.IDENTITY_CORRECTION
                and item.recompute_status
                is RecomputeStatus.REQUIRED
            }
            if any(
                item.performance_id in invalid_edge_ids
                for item in active
            ):
                raise OpponentIntelligenceError(
                    "restated graph contains identity-invalidated edge; "
                    "publish a corrected performance first"
                )
        return tuple(active)

    def _invalidate_targets_for_performance(
        self,
        performance_id: str,
        *,
        reason: InvalidationReason,
        evidence_id: str,
        detected_at: str,
        target: dict[str, DownstreamInvalidation],
    ) -> None:
        edge = self._make_invalidation(
            InvalidationTarget.OPPONENT_EDGE,
            performance_id,
            reason,
            evidence_id,
            detected_at,
        )
        target[edge.invalidation_id] = edge
        for rating in self._ratings.values():
            if performance_id not in rating.input_performance_ids:
                continue
            rating_invalidation = self._make_invalidation(
                InvalidationTarget.RATING_SNAPSHOT,
                rating.snapshot_id,
                reason,
                evidence_id,
                detected_at,
            )
            target[rating_invalidation.invalidation_id] = (
                rating_invalidation
            )
            for feature in self._features.values():
                if feature.rating_snapshot_id != rating.snapshot_id:
                    continue
                feature_invalidation = self._make_invalidation(
                    InvalidationTarget.FEATURE_SNAPSHOT,
                    feature.snapshot_id,
                    reason,
                    evidence_id,
                    detected_at,
                )
                target[feature_invalidation.invalidation_id] = (
                    feature_invalidation
                )

    @staticmethod
    def _make_invalidation(
        target_kind: InvalidationTarget,
        target_id: str,
        reason: InvalidationReason,
        evidence_id: str,
        detected_at: str,
    ) -> DownstreamInvalidation:
        payload = {
            "target_kind": target_kind.value,
            "target_id": _sha256("target_id", target_id),
            "reason": reason.value,
            "evidence_id": _sha256("evidence_id", evidence_id),
            "detected_at": _time_text(
                "detected_at", detected_at
            ),
            "recompute_status": RecomputeStatus.REQUIRED.value,
        }
        return DownstreamInvalidation(
            _digest(payload),
            target_kind,
            payload["target_id"],
            reason,
            payload["evidence_id"],
            payload["detected_at"],
        )

    def _persist_state(
        self,
        *,
        performances: dict[str, PerformanceRecord] | None = None,
        ratings: dict[str, RatingSnapshot] | None = None,
        features: dict[str, FeatureSnapshot] | None = None,
        invalidations: dict[str, DownstreamInvalidation] | None = None,
    ) -> None:
        performance_state = (
            self._performances
            if performances is None
            else performances
        )
        rating_state = (
            self._ratings if ratings is None else ratings
        )
        feature_state = (
            self._features if features is None else features
        )
        invalidation_state = (
            self._invalidations
            if invalidations is None
            else invalidations
        )
        atomic_write_json(
            self.path,
            {
                "schema": _SCHEMA,
                "version": _VERSION,
                "performances": [
                    item.payload()
                    for item in sorted(
                        performance_state.values(),
                        key=lambda item: item.performance_id,
                    )
                ],
                "rating_snapshots": [
                    item.payload()
                    for item in sorted(
                        rating_state.values(),
                        key=lambda item: item.snapshot_id,
                    )
                ],
                "feature_snapshots": [
                    item.payload()
                    for item in sorted(
                        feature_state.values(),
                        key=lambda item: item.snapshot_id,
                    )
                ],
                "invalidations": [
                    item.payload()
                    for item in sorted(
                        invalidation_state.values(),
                        key=lambda item: item.invalidation_id,
                    )
                ],
            },
        )

    def _persist(self) -> None:
        self._persist_state()

    def _load(self) -> None:
        try:
            raw = json.loads(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise OpponentIntelligenceError(
                f"cannot load opponent intelligence store: {exc}"
            ) from exc
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != _SCHEMA
            or raw.get("version") != _VERSION
        ):
            raise OpponentIntelligenceError(
                "unsupported opponent intelligence schema"
            )
        try:
            performances: dict[str, PerformanceRecord] = {}
            for item in raw.get("performances", []):
                observation = ObservedPerformance(
                    **item["observation"]
                )
                record = PerformanceRecord(
                    item["performance_id"],
                    observation,
                    item["subject_entity_id"],
                    item["opponent_entity_id"],
                    item["league_entity_id"],
                    item["subject_alias_record_id"],
                    item["opponent_alias_record_id"],
                    item["league_alias_record_id"],
                )
                if record.performance_id in performances:
                    raise OpponentIntelligenceError(
                        "duplicate performance record"
                    )
                performances[record.performance_id] = record

            ratings: dict[str, RatingSnapshot] = {}
            for item in raw.get("rating_snapshots", []):
                snapshot = RatingSnapshot(
                    item["snapshot_id"],
                    item["participant_entity_id"],
                    item["sport_id"],
                    item["league_id"],
                    item["market_context_id"],
                    IdentityView(item["view"]),
                    item["causal_cutoff"],
                    item["published_at"],
                    item["algorithm_family"],
                    item["algorithm_version"],
                    item["config_sha256"],
                    item["code_sha256"],
                    item["dependency_sha256"],
                    tuple(item["input_performance_ids"]),
                    item["input_digest"],
                    item["support"],
                    item["opponent_count"],
                    item["rating"],
                    item["uncertainty"],
                    SnapshotState(item["state"]),
                )
                if _digest(
                    snapshot.payload(include_id=False)
                ) != snapshot.snapshot_id:
                    raise OpponentIntelligenceError(
                        "rating snapshot digest mismatch"
                    )
                ratings[snapshot.snapshot_id] = snapshot

            features: dict[str, FeatureSnapshot] = {}
            for item in raw.get("feature_snapshots", []):
                snapshot = FeatureSnapshot(
                    item["snapshot_id"],
                    item["rating_snapshot_id"],
                    item["participant_entity_id"],
                    item["sport_id"],
                    item["league_id"],
                    item["market_context_id"],
                    IdentityView(item["view"]),
                    item["causal_cutoff"],
                    item["published_at"],
                    item["input_digest"],
                    item["support"],
                    item["opponent_count"],
                    item["last_observed_at"],
                    item["age_seconds"],
                    SnapshotState(item["state"]),
                )
                if _digest(
                    snapshot.payload(include_id=False)
                ) != snapshot.snapshot_id:
                    raise OpponentIntelligenceError(
                        "feature snapshot digest mismatch"
                    )
                if snapshot.rating_snapshot_id not in ratings:
                    raise OpponentIntelligenceError(
                        "feature snapshot references missing rating snapshot"
                    )
                features[snapshot.snapshot_id] = snapshot

            invalidations: dict[str, DownstreamInvalidation] = {}
            for item in raw.get("invalidations", []):
                invalidation = DownstreamInvalidation(
                    item["invalidation_id"],
                    InvalidationTarget(item["target_kind"]),
                    item["target_id"],
                    InvalidationReason(item["reason"]),
                    item["evidence_id"],
                    item["detected_at"],
                    RecomputeStatus(item["recompute_status"]),
                )
                if _digest(
                    invalidation.payload(include_id=False)
                ) != invalidation.invalidation_id:
                    raise OpponentIntelligenceError(
                        "invalidation digest mismatch"
                    )
                targets = {
                    InvalidationTarget.OPPONENT_EDGE: performances,
                    InvalidationTarget.RATING_SNAPSHOT: ratings,
                    InvalidationTarget.FEATURE_SNAPSHOT: features,
                }[invalidation.target_kind]
                if invalidation.target_id not in targets:
                    raise OpponentIntelligenceError(
                        "invalidation references missing target"
                    )
                invalidations[
                    invalidation.invalidation_id
                ] = invalidation
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, OpponentIntelligenceError):
                raise
            raise OpponentIntelligenceError(
                f"invalid opponent intelligence state: {exc}"
            ) from exc

        self._performances = performances
        self._ratings = ratings
        self._features = features
        self._invalidations = invalidations
