"""Causal opponent graph and deterministic rating-snapshot evidence.

This module consumes the canonical participant identity registry.  It does not
create aliases, infer identity from names, promote models, place stakes, or grant
provider/execution authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from pathlib import Path
from typing import Any

from .integrity import atomic_write_json
from .participant_identity import (
    EntityKind,
    EntityLineage,
    IdentityView,
    LineageRelation,
    ParticipantIdentityError,
    ParticipantIdentityRegistry,
)


_SCHEMA = "autosport.opponent_graph"
_VERSION = 1
_ALGORITHM_FAMILY = "LINEAR_PAIRWISE"
_ALGORITHM_VERSION = "1"
_INITIAL_RATING = Decimal("1500")
_K_FACTOR = Decimal("32")
_RATING_SPAN = Decimal("800")
_EXPECTED_FLOOR = Decimal("0.05")
_EXPECTED_CEILING = Decimal("0.95")
_UNCERTAINTY_SCALE = Decimal("400")
_ALLOWED_SCORES = frozenset({Decimal("0"), Decimal("0.5"), Decimal("1")})
_SHA256_HEX = frozenset("0123456789abcdef")


class OpponentGraphError(ValueError):
    """Raised when causal graph or snapshot evidence is invalid or ambiguous."""


class SnapshotStatus(StrEnum):
    READY = "READY"
    INSUFFICIENT = "INSUFFICIENT"
    STALE = "STALE"


class RecomputeCause(StrEnum):
    OUTCOME_CORRECTION = "OUTCOME_CORRECTION"
    IDENTITY_LINEAGE = "IDENTITY_LINEAGE"


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise OpponentGraphError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _instant(name: str, value: object) -> datetime:
    raw = _text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OpponentGraphError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OpponentGraphError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _time_text(name: str, value: object) -> str:
    return _instant(name, value).isoformat().replace("+00:00", "Z")


def _sha256(name: str, value: object) -> str:
    digest = _text(name, value)
    if len(digest) != 64 or digest != digest.lower() or any(ch not in _SHA256_HEX for ch in digest):
        raise OpponentGraphError(f"{name} must be lowercase SHA-256 hex")
    return digest


def _decimal(name: str, value: object) -> Decimal:
    if type(value) is not str or not value or value != value.strip():
        raise OpponentGraphError(f"{name} must be an exact decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise OpponentGraphError(f"{name} must be an exact decimal string") from exc
    if not result.is_finite():
        raise OpponentGraphError(f"{name} must be finite")
    return result


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise OpponentGraphError("decimal value must be finite")
    normalized = value.normalize()
    text = format(normalized, "f")
    return "0" if text in ("-0", "") else text


def _digest(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ObservedPerformance:
    """One authoritative observed pairwise outcome.

    score is from participant_id's perspective and is deliberately restricted to
    win/draw/loss truth: 1, 0.5, or 0.
    """

    outcome_id: str
    event_id: str
    source_id: str
    sport: str
    league_id: str
    participant_id: str
    opponent_id: str
    score: str
    observed_at: str
    available_at: str
    recorded_at: str
    evidence_sha256: str
    supersedes_outcome_id: str | None = None
    correction_reason: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "outcome_id", "event_id", "source_id", "sport", "league_id",
            "participant_id", "opponent_id",
        ):
            _text(name, getattr(self, name))
        if self.participant_id == self.opponent_id:
            raise OpponentGraphError("participant_id and opponent_id must differ")
        score = _decimal("score", self.score)
        if score not in _ALLOWED_SCORES:
            raise OpponentGraphError("score must be exactly 0, 0.5, or 1")
        observed = _instant("observed_at", self.observed_at)
        available = _instant("available_at", self.available_at)
        recorded = _instant("recorded_at", self.recorded_at)
        if available < observed:
            raise OpponentGraphError("outcome cannot be available before observed_at")
        if recorded < available:
            raise OpponentGraphError("outcome cannot be recorded before available_at")
        _sha256("evidence_sha256", self.evidence_sha256)
        if self.supersedes_outcome_id is None:
            if self.correction_reason is not None:
                raise OpponentGraphError("correction_reason requires supersedes_outcome_id")
        else:
            _text("supersedes_outcome_id", self.supersedes_outcome_id)
            if self.supersedes_outcome_id == self.outcome_id:
                raise OpponentGraphError("outcome cannot supersede itself")
            _text("correction_reason", self.correction_reason)

    @property
    def record_id(self) -> str:
        return _digest(self.payload())

    def payload(self) -> dict[str, str | None]:
        return {
            "outcome_id": self.outcome_id,
            "event_id": self.event_id,
            "source_id": self.source_id,
            "sport": self.sport,
            "league_id": self.league_id,
            "participant_id": self.participant_id,
            "opponent_id": self.opponent_id,
            "score": _decimal_text(_decimal("score", self.score)),
            "observed_at": _time_text("observed_at", self.observed_at),
            "available_at": _time_text("available_at", self.available_at),
            "recorded_at": _time_text("recorded_at", self.recorded_at),
            "evidence_sha256": self.evidence_sha256,
            "supersedes_outcome_id": self.supersedes_outcome_id,
            "correction_reason": self.correction_reason,
        }


@dataclass(frozen=True, slots=True)
class OpponentEdge:
    entity_id: str
    opponent_id: str
    sport: str
    league_id: str
    causal_cutoff: str
    view: IdentityView
    support_count: int
    wins: int
    draws: int
    losses: int
    input_outcome_ids: tuple[str, ...]
    input_digest: str

    def __post_init__(self) -> None:
        for name in ("entity_id", "opponent_id", "sport", "league_id"):
            _text(name, getattr(self, name))
        _instant("causal_cutoff", self.causal_cutoff)
        if not isinstance(self.view, IdentityView):
            raise TypeError("view must be IdentityView")
        if type(self.support_count) is not int or self.support_count <= 0:
            raise OpponentGraphError("support_count must be positive")
        for name in ("wins", "draws", "losses"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise OpponentGraphError(f"{name} must be a non-negative integer")
        if self.wins + self.draws + self.losses != self.support_count:
            raise OpponentGraphError("edge result counts must equal support_count")
        if len(self.input_outcome_ids) != self.support_count or len(set(self.input_outcome_ids)) != self.support_count:
            raise OpponentGraphError("edge inputs must bind each supporting outcome exactly once")
        _sha256("input_digest", self.input_digest)


@dataclass(frozen=True, slots=True)
class RatingSnapshot:
    participant_id: str
    sport: str
    league_id: str
    view: IdentityView
    status: SnapshotStatus
    causal_cutoff: str
    algorithm_family: str
    algorithm_version: str
    implementation_identity: str
    config_digest: str
    input_outcome_ids: tuple[str, ...]
    input_digest: str
    support_count: int
    rating: str | None
    uncertainty: str | None
    staleness_seconds: int | None
    predecessor_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "participant_id", "sport", "league_id", "algorithm_family",
            "algorithm_version", "implementation_identity",
        ):
            _text(name, getattr(self, name))
        if not isinstance(self.view, IdentityView):
            raise TypeError("view must be IdentityView")
        if not isinstance(self.status, SnapshotStatus):
            raise TypeError("status must be SnapshotStatus")
        _instant("causal_cutoff", self.causal_cutoff)
        _sha256("config_digest", self.config_digest)
        _sha256("input_digest", self.input_digest)
        if type(self.input_outcome_ids) is not tuple or len(set(self.input_outcome_ids)) != len(self.input_outcome_ids):
            raise OpponentGraphError("input_outcome_ids must be a unique canonical tuple")
        if type(self.support_count) is not int or self.support_count < 0:
            raise OpponentGraphError("support_count must be a non-negative integer")
        if self.status is SnapshotStatus.READY:
            _decimal("rating", self.rating)
            uncertainty = _decimal("uncertainty", self.uncertainty)
            if uncertainty < 0:
                raise OpponentGraphError("uncertainty must be non-negative")
            if type(self.staleness_seconds) is not int or self.staleness_seconds < 0:
                raise OpponentGraphError("READY snapshot requires non-negative staleness_seconds")
        else:
            if self.rating is not None or self.uncertainty is not None:
                raise OpponentGraphError("non-READY snapshot must not publish exact rating/uncertainty")
        if self.staleness_seconds is not None and (
            type(self.staleness_seconds) is not int or self.staleness_seconds < 0
        ):
            raise OpponentGraphError("staleness_seconds must be non-negative")
        if self.predecessor_snapshot_id is not None:
            _sha256("predecessor_snapshot_id", self.predecessor_snapshot_id)

    @property
    def snapshot_id(self) -> str:
        return _digest(self.payload())

    def payload(self) -> dict[str, object]:
        return {
            "participant_id": self.participant_id,
            "sport": self.sport,
            "league_id": self.league_id,
            "view": self.view.value,
            "status": self.status.value,
            "causal_cutoff": _time_text("causal_cutoff", self.causal_cutoff),
            "algorithm_family": self.algorithm_family,
            "algorithm_version": self.algorithm_version,
            "implementation_identity": self.implementation_identity,
            "config_digest": self.config_digest,
            "input_outcome_ids": list(self.input_outcome_ids),
            "input_digest": self.input_digest,
            "support_count": self.support_count,
            "rating": self.rating,
            "uncertainty": self.uncertainty,
            "staleness_seconds": self.staleness_seconds,
            "predecessor_snapshot_id": self.predecessor_snapshot_id,
        }

    def to_dict(self) -> dict[str, object]:
        return {"snapshot_id": self.snapshot_id, **self.payload()}


@dataclass(frozen=True, slots=True)
class RecomputationRequest:
    cause: RecomputeCause
    cause_id: str
    available_at: str
    affected_outcome_ids: tuple[str, ...]
    affected_snapshot_ids: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.cause, RecomputeCause):
            raise TypeError("cause must be RecomputeCause")
        _text("cause_id", self.cause_id)
        _instant("available_at", self.available_at)
        _text("reason", self.reason)
        if len(set(self.affected_outcome_ids)) != len(self.affected_outcome_ids):
            raise OpponentGraphError("affected_outcome_ids must be unique")
        if len(set(self.affected_snapshot_ids)) != len(self.affected_snapshot_ids):
            raise OpponentGraphError("affected_snapshot_ids must be unique")
        for value in self.affected_outcome_ids:
            _text("affected_outcome_id", value)
        for value in self.affected_snapshot_ids:
            _sha256("affected_snapshot_id", value)

    @property
    def request_id(self) -> str:
        return _digest(self.payload())

    def payload(self) -> dict[str, object]:
        return {
            "cause": self.cause.value,
            "cause_id": self.cause_id,
            "available_at": _time_text("available_at", self.available_at),
            "affected_outcome_ids": list(self.affected_outcome_ids),
            "affected_snapshot_ids": list(self.affected_snapshot_ids),
            "reason": self.reason,
        }

    def to_dict(self) -> dict[str, object]:
        return {"request_id": self.request_id, **self.payload()}


class OpponentGraphStore:
    """Append-only observed performance, opponent graph, and snapshot evidence."""

    def __init__(self, path: Path, identity_registry: ParticipantIdentityRegistry):
        if not isinstance(identity_registry, ParticipantIdentityRegistry):
            raise TypeError("identity_registry must be ParticipantIdentityRegistry")
        self.path = Path(path)
        self.identity_registry = identity_registry
        self._outcomes: dict[str, ObservedPerformance] = {}
        self._snapshots: dict[str, RatingSnapshot] = {}
        self._recomputations: dict[str, RecomputationRequest] = {}
        if self.path.exists():
            self._load()

    @classmethod
    def initialize_pristine(
        cls, path: Path, identity_registry: ParticipantIdentityRegistry
    ) -> "OpponentGraphStore":
        store = cls(path, identity_registry)
        if store.path.exists():
            raise OpponentGraphError("opponent graph store already exists")
        store._persist()
        return store

    def add_outcome(self, outcome: ObservedPerformance) -> None:
        if not isinstance(outcome, ObservedPerformance):
            raise TypeError("outcome must be ObservedPerformance")
        existing = self._outcomes.get(outcome.outcome_id)
        if existing is not None:
            if existing != outcome:
                raise OpponentGraphError("conflicting immutable outcome_id")
            return

        predecessor: ObservedPerformance | None = None
        self._validate_identity_binding(outcome, correction=outcome.supersedes_outcome_id is not None)
        if outcome.supersedes_outcome_id is not None:
            predecessor = self._outcomes.get(outcome.supersedes_outcome_id)
            if predecessor is None:
                raise OpponentGraphError("outcome correction references unknown predecessor")
            if any(item.supersedes_outcome_id == predecessor.outcome_id for item in self._outcomes.values()):
                raise OpponentGraphError("outcome correction fork is not allowed")
            if outcome.event_id != predecessor.event_id or outcome.source_id != predecessor.source_id:
                raise OpponentGraphError("outcome correction must preserve event/source identity")
            if outcome.sport != predecessor.sport:
                raise OpponentGraphError("outcome correction must preserve sport")
            if _instant("available_at", outcome.available_at) <= _instant("available_at", predecessor.available_at):
                raise OpponentGraphError("outcome correction must become available after predecessor")
            if _instant("recorded_at", outcome.recorded_at) <= _instant("recorded_at", predecessor.recorded_at):
                raise OpponentGraphError("outcome correction must be recorded after predecessor")
            self._validate_identity_correction(predecessor, outcome)

        candidate_outcomes = dict(self._outcomes)
        candidate_outcomes[outcome.outcome_id] = outcome
        candidate_recomputations = dict(self._recomputations)
        if predecessor is not None:
            affected_snapshots = tuple(sorted(
                snapshot_id
                for snapshot_id, snapshot in self._snapshots.items()
                if predecessor.outcome_id in snapshot.input_outcome_ids
            ))
            request = RecomputationRequest(
                cause=RecomputeCause.OUTCOME_CORRECTION,
                cause_id=outcome.record_id,
                available_at=outcome.available_at,
                affected_outcome_ids=(predecessor.outcome_id,),
                affected_snapshot_ids=affected_snapshots,
                reason="authoritative outcome correction requires restated downstream evidence",
            )
            candidate_recomputations[request.request_id] = request

        self._persist_state(outcomes=candidate_outcomes, recomputations=candidate_recomputations)
        self._outcomes = candidate_outcomes
        self._recomputations = candidate_recomputations

    def record_identity_lineage(self, lineage: EntityLineage) -> RecomputationRequest:
        if not isinstance(lineage, EntityLineage):
            raise TypeError("lineage must be EntityLineage")
        matching = {
            item.record_id
            for item in self.identity_registry.lineage_at(
                lineage.predecessor_entity_id,
                as_of=lineage.effective_from,
                view=IdentityView.RESTATED_RESEARCH,
            )
        }
        if lineage.record_id not in matching:
            raise OpponentGraphError("identity lineage is not present in canonical identity authority")

        affected_outcomes = []
        for outcome in self._outcomes.values():
            if lineage.predecessor_entity_id not in (
                outcome.participant_id, outcome.opponent_id, outcome.league_id
            ):
                continue
            if lineage.record_id in {
                item.record_id
                for item in self.identity_registry.lineage_at(
                    lineage.predecessor_entity_id,
                    as_of=outcome.observed_at,
                    view=IdentityView.RESTATED_RESEARCH,
                )
            }:
                affected_outcomes.append(outcome.outcome_id)

        affected_set = set(affected_outcomes)
        affected_snapshots = tuple(sorted(
            snapshot_id
            for snapshot_id, snapshot in self._snapshots.items()
            if (
                snapshot.participant_id in (
                    lineage.predecessor_entity_id, lineage.successor_entity_id
                )
                or snapshot.league_id in (
                    lineage.predecessor_entity_id, lineage.successor_entity_id
                )
                or bool(affected_set.intersection(snapshot.input_outcome_ids))
            )
        ))
        request = RecomputationRequest(
            cause=RecomputeCause.IDENTITY_LINEAGE,
            cause_id=lineage.record_id,
            available_at=lineage.available_at,
            affected_outcome_ids=tuple(sorted(affected_outcomes)),
            affected_snapshot_ids=affected_snapshots,
            reason="canonical identity lineage requires explicit restated graph/snapshot recomputation",
        )
        if request.request_id in self._recomputations:
            return self._recomputations[request.request_id]
        candidate = dict(self._recomputations)
        candidate[request.request_id] = request
        self._persist_state(recomputations=candidate)
        self._recomputations = candidate
        return request

    def opponent_edges(
        self,
        entity_id: str,
        *,
        sport: str,
        league_id: str,
        causal_cutoff: str,
        view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
    ) -> tuple[OpponentEdge, ...]:
        canonical_entity = _text("entity_id", entity_id)
        canonical_sport = _text("sport", sport)
        canonical_league = _text("league_id", league_id)
        self._validate_query_identity(canonical_entity, canonical_league, causal_cutoff, view)
        active = self._active_outcomes(causal_cutoff, view=view)
        self._assert_identity_recomputation_resolved(active, causal_cutoff, view)
        grouped: dict[str, list[tuple[ObservedPerformance, Decimal]]] = {}
        for outcome in active:
            if outcome.sport != canonical_sport or outcome.league_id != canonical_league:
                continue
            if outcome.participant_id == canonical_entity:
                grouped.setdefault(outcome.opponent_id, []).append(
                    (outcome, _decimal("score", outcome.score))
                )
            elif outcome.opponent_id == canonical_entity:
                grouped.setdefault(outcome.participant_id, []).append(
                    (outcome, Decimal("1") - _decimal("score", outcome.score))
                )

        edges = []
        for opponent_id, records in grouped.items():
            records.sort(key=lambda item: self._outcome_order(item[0], view))
            outcomes = [item[0] for item in records]
            scores = [item[1] for item in records]
            edges.append(OpponentEdge(
                entity_id=canonical_entity,
                opponent_id=opponent_id,
                sport=canonical_sport,
                league_id=canonical_league,
                causal_cutoff=_time_text("causal_cutoff", causal_cutoff),
                view=view,
                support_count=len(records),
                wins=sum(score == Decimal("1") for score in scores),
                draws=sum(score == Decimal("0.5") for score in scores),
                losses=sum(score == Decimal("0") for score in scores),
                input_outcome_ids=tuple(item.outcome_id for item in outcomes),
                input_digest=self._input_digest(outcomes),
            ))
        return tuple(sorted(edges, key=lambda edge: edge.opponent_id))

    def build_snapshot(
        self,
        participant_id: str,
        *,
        sport: str,
        league_id: str,
        causal_cutoff: str,
        view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
        min_support: int = 3,
        stale_after_seconds: int = 7 * 24 * 60 * 60,
    ) -> RatingSnapshot:
        if type(min_support) is not int or min_support <= 0:
            raise OpponentGraphError("min_support must be a positive integer")
        if type(stale_after_seconds) is not int or stale_after_seconds < 0:
            raise OpponentGraphError("stale_after_seconds must be a non-negative integer")
        canonical_participant = _text("participant_id", participant_id)
        canonical_sport = _text("sport", sport)
        canonical_league = _text("league_id", league_id)
        cutoff = _time_text("causal_cutoff", causal_cutoff)
        self._validate_query_identity(canonical_participant, canonical_league, cutoff, view)

        active = self._active_outcomes(cutoff, view=view)
        self._assert_identity_recomputation_resolved(active, cutoff, view)
        context = [
            outcome for outcome in active
            if outcome.sport == canonical_sport and outcome.league_id == canonical_league
        ]
        context.sort(key=lambda outcome: self._outcome_order(outcome, view))

        config_digest = _digest({
            "family": _ALGORITHM_FAMILY,
            "version": _ALGORITHM_VERSION,
            "initial_rating": _decimal_text(_INITIAL_RATING),
            "k_factor": _decimal_text(_K_FACTOR),
            "rating_span": _decimal_text(_RATING_SPAN),
            "expected_floor": _decimal_text(_EXPECTED_FLOOR),
            "expected_ceiling": _decimal_text(_EXPECTED_CEILING),
            "uncertainty_scale": _decimal_text(_UNCERTAINTY_SCALE),
            "min_support": min_support,
            "stale_after_seconds": stale_after_seconds,
        })
        ratings: dict[str, Decimal] = {}
        support: dict[str, int] = {}
        last_observed: dict[str, datetime] = {}
        with localcontext() as context_precision:
            context_precision.prec = 28
            for outcome in context:
                participant_rating = ratings.get(outcome.participant_id, _INITIAL_RATING)
                opponent_rating = ratings.get(outcome.opponent_id, _INITIAL_RATING)
                expected = Decimal("0.5") + (
                    participant_rating - opponent_rating
                ) / _RATING_SPAN
                expected = max(_EXPECTED_FLOOR, min(_EXPECTED_CEILING, expected))
                delta = _K_FACTOR * (_decimal("score", outcome.score) - expected)
                ratings[outcome.participant_id] = participant_rating + delta
                ratings[outcome.opponent_id] = opponent_rating - delta
                support[outcome.participant_id] = support.get(outcome.participant_id, 0) + 1
                support[outcome.opponent_id] = support.get(outcome.opponent_id, 0) + 1
                observed = _instant("observed_at", outcome.observed_at)
                last_observed[outcome.participant_id] = max(
                    observed, last_observed.get(outcome.participant_id, observed)
                )
                last_observed[outcome.opponent_id] = max(
                    observed, last_observed.get(outcome.opponent_id, observed)
                )

        participant_support = support.get(canonical_participant, 0)
        staleness_seconds: int | None = None
        rating: str | None = None
        uncertainty: str | None = None
        if participant_support < min_support:
            status = SnapshotStatus.INSUFFICIENT
        else:
            latest = last_observed[canonical_participant]
            staleness_seconds = max(
                0, int((_instant("causal_cutoff", cutoff) - latest).total_seconds())
            )
            if staleness_seconds > stale_after_seconds:
                status = SnapshotStatus.STALE
            else:
                status = SnapshotStatus.READY
                rating_value = ratings[canonical_participant].quantize(Decimal("0.0001"))
                with localcontext() as context_precision:
                    context_precision.prec = 28
                    uncertainty_value = (
                        _UNCERTAINTY_SCALE / Decimal(participant_support).sqrt()
                    ).quantize(Decimal("0.0001"))
                rating = _decimal_text(rating_value)
                uncertainty = _decimal_text(uncertainty_value)

        predecessor = self._snapshot_predecessor(
            canonical_participant, canonical_sport, canonical_league, cutoff, view
        )
        snapshot = RatingSnapshot(
            participant_id=canonical_participant,
            sport=canonical_sport,
            league_id=canonical_league,
            view=view,
            status=status,
            causal_cutoff=cutoff,
            algorithm_family=_ALGORITHM_FAMILY,
            algorithm_version=_ALGORITHM_VERSION,
            implementation_identity="autosport.opponent_graph:linear_pairwise_v1",
            config_digest=config_digest,
            input_outcome_ids=tuple(item.outcome_id for item in context),
            input_digest=self._input_digest(context),
            support_count=participant_support,
            rating=rating,
            uncertainty=uncertainty,
            staleness_seconds=staleness_seconds,
            predecessor_snapshot_id=predecessor,
        )
        existing = self._snapshots.get(snapshot.snapshot_id)
        if existing is not None:
            return existing
        candidate = dict(self._snapshots)
        candidate[snapshot.snapshot_id] = snapshot
        self._persist_state(snapshots=candidate)
        self._snapshots = candidate
        return snapshot

    def get_snapshot(self, snapshot_id: str) -> RatingSnapshot:
        canonical_id = _sha256("snapshot_id", snapshot_id)
        try:
            return self._snapshots[canonical_id]
        except KeyError as exc:
            raise OpponentGraphError("unknown snapshot_id") from exc

    def recomputation_requests(self) -> tuple[RecomputationRequest, ...]:
        return tuple(self._recomputations[key] for key in sorted(self._recomputations))

    def invalidations_for_snapshot(self, snapshot_id: str) -> tuple[RecomputationRequest, ...]:
        canonical_id = _sha256("snapshot_id", snapshot_id)
        return tuple(
            request for request in self.recomputation_requests()
            if canonical_id in request.affected_snapshot_ids
        )

    def _validate_query_identity(
        self, participant_id: str, league_id: str, causal_cutoff: str, view: IdentityView
    ) -> None:
        if not isinstance(view, IdentityView):
            raise TypeError("view must be IdentityView")
        try:
            participant = self.identity_registry.entity_at(
                participant_id, as_of=causal_cutoff, view=view
            )
            league = self.identity_registry.entity_at(
                league_id, as_of=causal_cutoff, view=view
            )
        except ParticipantIdentityError as exc:
            raise OpponentGraphError(f"identity authority rejected query: {exc}") from exc
        if participant.kind not in (EntityKind.PARTICIPANT, EntityKind.TEAM):
            raise OpponentGraphError("rating entity must be PARTICIPANT or TEAM")
        if league.kind is not EntityKind.LEAGUE:
            raise OpponentGraphError("league_id must reference canonical LEAGUE identity")

    def _validate_identity_binding(self, outcome: ObservedPerformance, *, correction: bool) -> None:
        view = IdentityView.RESTATED_RESEARCH if correction else IdentityView.AS_KNOWN_AT_DECISION
        as_of = outcome.observed_at if correction else outcome.available_at
        try:
            participant = self.identity_registry.entity_at(outcome.participant_id, as_of=as_of, view=view)
            opponent = self.identity_registry.entity_at(outcome.opponent_id, as_of=as_of, view=view)
            league = self.identity_registry.entity_at(outcome.league_id, as_of=as_of, view=view)
            roster = self.identity_registry.roster_at(
                outcome.event_id, outcome.source_id, as_of=outcome.observed_at, view=view
            )
        except ParticipantIdentityError as exc:
            raise OpponentGraphError(f"identity authority rejected outcome: {exc}") from exc
        if participant.kind not in (EntityKind.PARTICIPANT, EntityKind.TEAM):
            raise OpponentGraphError("outcome participant must be PARTICIPANT or TEAM")
        if opponent.kind is not participant.kind:
            raise OpponentGraphError("participant/opponent kinds must match")
        if league.kind is not EntityKind.LEAGUE:
            raise OpponentGraphError("league_id must reference canonical LEAGUE identity")
        roster_ids = {entity.entity_id for entity in roster}
        if outcome.participant_id not in roster_ids or outcome.opponent_id not in roster_ids:
            raise OpponentGraphError("outcome identities must be bound by canonical event roster")

    def _validate_identity_correction(
        self, predecessor: ObservedPerformance, correction: ObservedPerformance
    ) -> None:
        for field in ("participant_id", "opponent_id", "league_id"):
            old = getattr(predecessor, field)
            new = getattr(correction, field)
            if old == new:
                continue
            lineages = self.identity_registry.lineage_at(
                old, as_of=predecessor.observed_at, view=IdentityView.RESTATED_RESEARCH
            )
            valid = [
                lineage for lineage in lineages
                if lineage.predecessor_entity_id == old
                and lineage.successor_entity_id == new
                and _instant("available_at", lineage.available_at)
                <= _instant("correction available_at", correction.available_at)
            ]
            if not valid:
                raise OpponentGraphError(
                    f"{field} correction requires canonical identity lineage"
                )

    def _active_outcomes(
        self, causal_cutoff: str, *, view: IdentityView
    ) -> list[ObservedPerformance]:
        if not isinstance(view, IdentityView):
            raise TypeError("view must be IdentityView")
        moment = _instant("causal_cutoff", causal_cutoff)
        if view is IdentityView.AS_KNOWN_AT_DECISION:
            candidates = [
                outcome for outcome in self._outcomes.values()
                if _instant("available_at", outcome.available_at) <= moment
                and _instant("recorded_at", outcome.recorded_at) <= moment
            ]
        else:
            candidates = [
                outcome for outcome in self._outcomes.values()
                if _instant("observed_at", outcome.observed_at) <= moment
            ]
        candidate_ids = {outcome.outcome_id for outcome in candidates}
        superseded = {
            outcome.supersedes_outcome_id
            for outcome in candidates
            if outcome.supersedes_outcome_id in candidate_ids
        }
        result = [outcome for outcome in candidates if outcome.outcome_id not in superseded]
        result.sort(key=lambda outcome: self._outcome_order(outcome, view))
        return result

    @staticmethod
    def _outcome_order(outcome: ObservedPerformance, view: IdentityView) -> tuple[str, str, str]:
        if view is IdentityView.AS_KNOWN_AT_DECISION:
            return (
                _time_text("available_at", outcome.available_at),
                _time_text("observed_at", outcome.observed_at),
                outcome.outcome_id,
            )
        return (
            _time_text("observed_at", outcome.observed_at),
            _time_text("available_at", outcome.available_at),
            outcome.outcome_id,
        )

    def _assert_identity_recomputation_resolved(
        self,
        active: list[ObservedPerformance],
        causal_cutoff: str,
        view: IdentityView,
    ) -> None:
        active_ids = {item.outcome_id for item in active}
        moment = _instant("causal_cutoff", causal_cutoff)
        for request in self._recomputations.values():
            if request.cause is not RecomputeCause.IDENTITY_LINEAGE:
                continue
            if (
                view is IdentityView.AS_KNOWN_AT_DECISION
                and _instant("available_at", request.available_at) > moment
            ):
                continue
            if active_ids.intersection(request.affected_outcome_ids):
                raise OpponentGraphError(
                    "identity correction requires explicit corrected outcome recomputation"
                )

    def _snapshot_predecessor(
        self,
        participant_id: str,
        sport: str,
        league_id: str,
        causal_cutoff: str,
        view: IdentityView,
    ) -> str | None:
        candidates = [
            snapshot for snapshot in self._snapshots.values()
            if snapshot.participant_id == participant_id
            and snapshot.sport == sport
            and snapshot.league_id == league_id
            and snapshot.causal_cutoff == causal_cutoff
        ]
        if not candidates:
            return None
        if view is IdentityView.RESTATED_RESEARCH:
            decision = [
                snapshot for snapshot in candidates
                if snapshot.view is IdentityView.AS_KNOWN_AT_DECISION
            ]
            if decision:
                return sorted(decision, key=lambda item: item.snapshot_id)[-1].snapshot_id
        same_view = [snapshot for snapshot in candidates if snapshot.view is view]
        return (
            sorted(same_view, key=lambda item: item.snapshot_id)[-1].snapshot_id
            if same_view else None
        )

    @staticmethod
    def _input_digest(outcomes: list[ObservedPerformance]) -> str:
        return _digest({
            "records": [
                {"outcome_id": outcome.outcome_id, "record_id": outcome.record_id}
                for outcome in outcomes
            ]
        })

    def _persist_state(
        self,
        *,
        outcomes: dict[str, ObservedPerformance] | None = None,
        snapshots: dict[str, RatingSnapshot] | None = None,
        recomputations: dict[str, RecomputationRequest] | None = None,
    ) -> None:
        outcome_state = self._outcomes if outcomes is None else outcomes
        snapshot_state = self._snapshots if snapshots is None else snapshots
        recompute_state = self._recomputations if recomputations is None else recomputations
        atomic_write_json(self.path, {
            "schema": _SCHEMA,
            "version": _VERSION,
            "outcomes": [
                outcome_state[key].payload() for key in sorted(outcome_state)
            ],
            "snapshots": [
                snapshot_state[key].to_dict() for key in sorted(snapshot_state)
            ],
            "recomputation_requests": [
                recompute_state[key].to_dict() for key in sorted(recompute_state)
            ],
        })

    def _persist(self) -> None:
        self._persist_state()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OpponentGraphError(f"cannot load opponent graph store: {exc}") from exc
        if (
            type(raw) is not dict
            or raw.get("schema") != _SCHEMA
            or raw.get("version") != _VERSION
            or set(raw) != {"schema", "version", "outcomes", "snapshots", "recomputation_requests"}
        ):
            raise OpponentGraphError("unsupported opponent graph store schema")

        outcomes: dict[str, ObservedPerformance] = {}
        for item in raw["outcomes"]:
            if type(item) is not dict:
                raise OpponentGraphError("serialized outcome must be an object")
            outcome = ObservedPerformance(**item)
            if outcome.outcome_id in outcomes:
                raise OpponentGraphError("duplicate serialized outcome_id")
            outcomes[outcome.outcome_id] = outcome
        self._outcomes = outcomes
        for outcome in outcomes.values():
            self._validate_identity_binding(
                outcome, correction=outcome.supersedes_outcome_id is not None
            )
            if outcome.supersedes_outcome_id is not None:
                predecessor = outcomes.get(outcome.supersedes_outcome_id)
                if predecessor is None:
                    raise OpponentGraphError("serialized correction references unknown predecessor")
                self._validate_identity_correction(predecessor, outcome)

        snapshots: dict[str, RatingSnapshot] = {}
        for item in raw["snapshots"]:
            if type(item) is not dict:
                raise OpponentGraphError("serialized snapshot must be an object")
            expected_id = item.get("snapshot_id")
            snapshot = RatingSnapshot(
                participant_id=item.get("participant_id"),
                sport=item.get("sport"),
                league_id=item.get("league_id"),
                view=IdentityView(item.get("view")),
                status=SnapshotStatus(item.get("status")),
                causal_cutoff=item.get("causal_cutoff"),
                algorithm_family=item.get("algorithm_family"),
                algorithm_version=item.get("algorithm_version"),
                implementation_identity=item.get("implementation_identity"),
                config_digest=item.get("config_digest"),
                input_outcome_ids=tuple(item.get("input_outcome_ids", ())),
                input_digest=item.get("input_digest"),
                support_count=item.get("support_count"),
                rating=item.get("rating"),
                uncertainty=item.get("uncertainty"),
                staleness_seconds=item.get("staleness_seconds"),
                predecessor_snapshot_id=item.get("predecessor_snapshot_id"),
            )
            if expected_id != snapshot.snapshot_id:
                raise OpponentGraphError("serialized snapshot_id does not match content")
            try:
                input_records = [outcomes[outcome_id] for outcome_id in snapshot.input_outcome_ids]
            except KeyError as exc:
                raise OpponentGraphError("snapshot references unknown outcome_id") from exc
            if snapshot.input_digest != self._input_digest(input_records):
                raise OpponentGraphError("snapshot input_digest does not match durable inputs")
            snapshots[snapshot.snapshot_id] = snapshot
        self._snapshots = snapshots

        recomputations: dict[str, RecomputationRequest] = {}
        for item in raw["recomputation_requests"]:
            if type(item) is not dict:
                raise OpponentGraphError("serialized recomputation request must be an object")
            expected_id = item.get("request_id")
            request = RecomputationRequest(
                cause=RecomputeCause(item.get("cause")),
                cause_id=item.get("cause_id"),
                available_at=item.get("available_at"),
                affected_outcome_ids=tuple(item.get("affected_outcome_ids", ())),
                affected_snapshot_ids=tuple(item.get("affected_snapshot_ids", ())),
                reason=item.get("reason"),
            )
            if expected_id != request.request_id:
                raise OpponentGraphError("serialized recomputation request_id does not match content")
            recomputations[request.request_id] = request
        self._recomputations = recomputations
