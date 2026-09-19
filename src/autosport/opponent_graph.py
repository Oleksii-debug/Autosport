"""Causal opponent graph and transparent rating/feature snapshots.

This module consumes the canonical participant identity registry as its only
identity authority.  It stores append-only observed performance evidence and
immutable derived snapshots; it never rewrites historical outcomes or grants
prediction, promotion, risk, or execution authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from pathlib import Path
from typing import Final

from .integrity import atomic_write_json
from .participant_identity import ParticipantIdentityRegistry, ParticipantIdentityError
from .workspace_lock import WorkspaceEconomicLock


_SCHEMA: Final = "autosport.opponent_graph"
_VERSION: Final = 1
_RATING_ALGORITHM: Final = "ELO"
_RATING_VERSION: Final = "1"
_INITIAL_RATING = Decimal("1500")
_DEFAULT_K = Decimal("20")
_DECIMAL_QUANT = Decimal("0.000001")


class OpponentGraphError(ValueError):
    """Raised when causal graph/rating evidence is malformed or unsafe."""


class IdentityView(StrEnum):
    AS_KNOWN_AT_DECISION = "AS_KNOWN_AT_DECISION"
    RESTATED_RESEARCH = "RESTATED_RESEARCH"


class MatchResult(StrEnum):
    A_WIN = "A_WIN"
    B_WIN = "B_WIN"
    DRAW = "DRAW"


class RecomputeStatus(StrEnum):
    REQUIRED = "REQUIRED"
    COMPLETED = "COMPLETED"


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise OpponentGraphError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _instant(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OpponentGraphError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OpponentGraphError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _time_text(name: str, value: object) -> str:
    return _instant(name, value).isoformat().replace("+00:00", "Z")


def _sha(name: str, value: object) -> str:
    text = _text(name, value).lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise OpponentGraphError(f"{name} must be lowercase SHA-256 hex")
    return text


def _decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise OpponentGraphError(f"{name} must be a finite Decimal")
    return value


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _round(value: Decimal) -> Decimal:
    return value.quantize(_DECIMAL_QUANT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class PerformanceOutcome:
    """One observed two-party performance fact, append-only and correction-aware."""

    event_id: str
    sport_id: str
    participant_a_id: str
    participant_b_id: str
    result: MatchResult
    observed_at: str
    available_at: str
    evidence_sha256: str
    league_id: str | None = None
    market_id: str | None = None
    supersedes_observation_id: str | None = None
    schema: str = _SCHEMA
    version: int = _VERSION

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA or self.version != _VERSION:
            raise OpponentGraphError("unsupported performance outcome schema")
        for name in (
            "event_id", "sport_id", "participant_a_id", "participant_b_id",
        ):
            _text(name, getattr(self, name))
        if self.participant_a_id == self.participant_b_id:
            raise OpponentGraphError("opponent outcome requires distinct participant IDs")
        if not isinstance(self.result, MatchResult):
            raise OpponentGraphError("result must be MatchResult")
        observed = _instant("observed_at", self.observed_at)
        available = _instant("available_at", self.available_at)
        if available < observed:
            raise OpponentGraphError("outcome cannot be available before observed_at")
        _sha("evidence_sha256", self.evidence_sha256)
        for name in ("league_id", "market_id"):
            value = getattr(self, name)
            if value is not None:
                _text(name, value)
        if self.supersedes_observation_id is not None:
            _sha("supersedes_observation_id", self.supersedes_observation_id)

    def payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "sport_id": self.sport_id,
            "participant_a_id": self.participant_a_id,
            "participant_b_id": self.participant_b_id,
            "result": self.result.value,
            "observed_at": _time_text("observed_at", self.observed_at),
            "available_at": _time_text("available_at", self.available_at),
            "evidence_sha256": self.evidence_sha256,
            "league_id": self.league_id,
            "market_id": self.market_id,
            "supersedes_observation_id": self.supersedes_observation_id,
            "schema": self.schema,
            "version": self.version,
        }

    @property
    def observation_id(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True, slots=True)
class OpponentEdge:
    """Derived immutable graph edge bound to one observed outcome."""

    observation_id: str
    participant_a_id: str
    participant_b_id: str
    event_id: str
    sport_id: str
    observed_at: str
    available_at: str
    result: MatchResult
    evidence_sha256: str

    @property
    def edge_id(self) -> str:
        ordered = sorted((self.participant_a_id, self.participant_b_id))
        return _digest(
            {
                "observation_id": self.observation_id,
                "participants": ordered,
                "event_id": self.event_id,
            }
        )


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    participant_id: str
    rating: Decimal
    uncertainty: Decimal
    support: int
    as_of: str
    view: IdentityView

    def __post_init__(self) -> None:
        _text("participant_id", self.participant_id)
        _decimal("rating", self.rating)
        _decimal("uncertainty", self.uncertainty)
        if isinstance(self.support, bool) or not isinstance(self.support, int) or self.support < 1:
            raise OpponentGraphError("support must be a positive integer")
        if self.uncertainty <= 0:
            raise OpponentGraphError("uncertainty must be positive")
        _instant("as_of", self.as_of)
        if not isinstance(self.view, IdentityView):
            raise OpponentGraphError("view must be IdentityView")

    def payload(self) -> dict[str, object]:
        return {
            "participant_id": self.participant_id,
            "rating": str(_round(self.rating)),
            "uncertainty": str(_round(self.uncertainty)),
            "support": self.support,
            "as_of": _time_text("as_of", self.as_of),
            "view": self.view.value,
        }

    @property
    def feature_id(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True, slots=True)
class RatingSnapshot:
    algorithm: str
    algorithm_version: str
    config_digest: str
    as_of: str
    view: IdentityView
    input_observation_ids: tuple[str, ...]
    input_set_digest: str
    features: tuple[FeatureSnapshot, ...]
    causal_cutoff: str
    invalidated: bool = False
    invalidation_work_id: str | None = None

    def __post_init__(self) -> None:
        if self.algorithm != _RATING_ALGORITHM or self.algorithm_version != _RATING_VERSION:
            raise OpponentGraphError("unsupported rating algorithm/version")
        _sha("config_digest", self.config_digest)
        _instant("as_of", self.as_of)
        _instant("causal_cutoff", self.causal_cutoff)
        if not isinstance(self.view, IdentityView):
            raise OpponentGraphError("view must be IdentityView")
        if not self.input_observation_ids:
            raise OpponentGraphError("rating snapshot requires at least one observation")
        if tuple(sorted(self.input_observation_ids)) != self.input_observation_ids:
            raise OpponentGraphError("input observation IDs must be sorted")
        if len(set(self.input_observation_ids)) != len(self.input_observation_ids):
            raise OpponentGraphError("duplicate input observation ID")
        _sha("input_set_digest", self.input_set_digest)
        if not self.features:
            raise OpponentGraphError("rating snapshot requires feature rows")
        if self.invalidation_work_id is not None:
            _sha("invalidation_work_id", self.invalidation_work_id)

    def payload(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm,
            "algorithm_version": self.algorithm_version,
            "config_digest": self.config_digest,
            "as_of": _time_text("as_of", self.as_of),
            "view": self.view.value,
            "input_observation_ids": list(self.input_observation_ids),
            "input_set_digest": self.input_set_digest,
            "features": [feature.payload() for feature in self.features],
            "causal_cutoff": _time_text("causal_cutoff", self.causal_cutoff),
            "invalidated": self.invalidated,
            "invalidation_work_id": self.invalidation_work_id,
        }

    @property
    def snapshot_id(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True, slots=True)
class RecomputeWork:
    target_snapshot_id: str
    reason_code: str
    status: RecomputeStatus
    created_from_observation_id: str
    affected_entity_ids: tuple[str, ...]
    work_id: str

    def __post_init__(self) -> None:
        _sha("target_snapshot_id", self.target_snapshot_id)
        _text("reason_code", self.reason_code)
        if not isinstance(self.status, RecomputeStatus):
            raise OpponentGraphError("invalid recompute status")
        _sha("created_from_observation_id", self.created_from_observation_id)
        if not self.affected_entity_ids or tuple(sorted(self.affected_entity_ids)) != self.affected_entity_ids:
            raise OpponentGraphError("affected_entity_ids must be sorted and non-empty")
        for entity_id in self.affected_entity_ids:
            _text("affected_entity_id", entity_id)
        _sha("work_id", self.work_id)

    @classmethod
    def create(
        cls,
        target_snapshot_id: str,
        reason_code: str,
        created_from_observation_id: str,
        affected_entity_ids: tuple[str, ...],
    ) -> "RecomputeWork":
        payload = {
            "target_snapshot_id": target_snapshot_id,
            "reason_code": reason_code,
            "created_from_observation_id": created_from_observation_id,
            "affected_entity_ids": list(sorted(set(affected_entity_ids))),
        }
        return cls(
            target_snapshot_id=target_snapshot_id,
            reason_code=reason_code,
            status=RecomputeStatus.REQUIRED,
            created_from_observation_id=created_from_observation_id,
            affected_entity_ids=tuple(sorted(set(affected_entity_ids))),
            work_id=_digest(payload),
        )

    def payload(self) -> dict[str, object]:
        return {
            "target_snapshot_id": self.target_snapshot_id,
            "reason_code": self.reason_code,
            "status": self.status.value,
            "created_from_observation_id": self.created_from_observation_id,
            "affected_entity_ids": list(self.affected_entity_ids),
            "work_id": self.work_id,
        }


class OpponentGraphStore:
    """Durable causal outcome/edge/snapshot store using one atomic JSON object."""

    def __init__(self, path: str | Path, identity_registry: ParticipantIdentityRegistry) -> None:
        self.path = Path(path)
        if not isinstance(identity_registry, ParticipantIdentityRegistry):
            raise TypeError("identity_registry must be ParticipantIdentityRegistry")
        self.identity_registry = identity_registry
        self._observations: dict[str, PerformanceOutcome] = {}
        self._edges: dict[str, OpponentEdge] = {}
        self._snapshots: dict[str, RatingSnapshot] = {}
        self._work: dict[str, RecomputeWork] = {}
        self._loading = False
        if self.path.exists():
            self._load()

    def _require_entity(self, entity_id: str) -> None:
        _text("entity_id", entity_id)
        if entity_id not in self.identity_registry._entities:
            raise OpponentGraphError(f"unknown canonical participant identity: {entity_id}")

    def add_observation(self, observation: PerformanceOutcome) -> str:
        if not isinstance(observation, PerformanceOutcome):
            raise TypeError("observation must be PerformanceOutcome")
        self._require_entity(observation.participant_a_id)
        self._require_entity(observation.participant_b_id)
        if observation.supersedes_observation_id is not None and observation.supersedes_observation_id not in self._observations:
            raise OpponentGraphError("correction predecessor is not durably present")
        existing = self._observations.get(observation.observation_id)
        if existing is not None:
            if existing != observation:
                raise OpponentGraphError("conflicting immutable observation identity")
            return observation.observation_id
        self._observations[observation.observation_id] = observation
        self._edges[OpponentEdge(
            observation_id=observation.observation_id,
            participant_a_id=observation.participant_a_id,
            participant_b_id=observation.participant_b_id,
            event_id=observation.event_id,
            sport_id=observation.sport_id,
            observed_at=observation.observed_at,
            available_at=observation.available_at,
            result=observation.result,
            evidence_sha256=observation.evidence_sha256,
        ).edge_id] = OpponentEdge(
            observation_id=observation.observation_id,
            participant_a_id=observation.participant_a_id,
            participant_b_id=observation.participant_b_id,
            event_id=observation.event_id,
            sport_id=observation.sport_id,
            observed_at=observation.observed_at,
            available_at=observation.available_at,
            result=observation.result,
            evidence_sha256=observation.evidence_sha256,
        )
        self._persist()
        if observation.supersedes_observation_id is not None:
            self.invalidate_for_correction(observation.supersedes_observation_id, observation.observation_id)
        return observation.observation_id

    def _eligible(
        self,
        *,
        as_of: str,
        view: IdentityView,
    ) -> tuple[PerformanceOutcome, ...]:
        cutoff = _instant("as_of", as_of)
        if not isinstance(view, IdentityView):
            raise TypeError("view must be IdentityView")
        rows = []
        for observation in self._observations.values():
            if view is IdentityView.AS_KNOWN_AT_DECISION:
                if _instant("observed_at", observation.observed_at) > cutoff:
                    continue
                if _instant("available_at", observation.available_at) > cutoff:
                    continue
            else:
                if _instant("observed_at", observation.observed_at) > cutoff:
                    continue
            rows.append(observation)
        rows.sort(key=lambda row: (_instant("observed_at", row.observed_at), row.observation_id))
        if view is IdentityView.RESTATED_RESEARCH:
            superseded = {
                row.supersedes_observation_id
                for row in rows
                if row.supersedes_observation_id is not None
            }
            rows = [row for row in rows if row.observation_id not in superseded]
        return tuple(rows)

    @staticmethod
    def _expected(result_a: MatchResult) -> tuple[Decimal, Decimal]:
        if result_a is MatchResult.A_WIN:
            return Decimal(1), Decimal(0)
        if result_a is MatchResult.B_WIN:
            return Decimal(0), Decimal(1)
        return Decimal("0.5"), Decimal("0.5")

    def build_rating_snapshot(
        self,
        *,
        as_of: str,
        view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
        k_factor: Decimal = _DEFAULT_K,
        stale_after_days: int = 90,
    ) -> RatingSnapshot:
        cutoff = _instant("as_of", as_of)
        k = _decimal("k_factor", k_factor)
        if k <= 0:
            raise OpponentGraphError("k_factor must be positive")
        if isinstance(stale_after_days, bool) or not isinstance(stale_after_days, int) or stale_after_days <= 0:
            raise OpponentGraphError("stale_after_days must be positive")
        rows = self._eligible(as_of=as_of, view=view)
        if not rows:
            raise OpponentGraphError("insufficient causal evidence for rating snapshot")

        config_digest = _digest(
            {
                "algorithm": _RATING_ALGORITHM,
                "version": _RATING_VERSION,
                "k_factor": str(k),
                "stale_after_days": stale_after_days,
            }
        )

        ratings: dict[str, Decimal] = {}
        support: dict[str, int] = {}
        last_available: dict[str, datetime] = {}
        for row in rows:
            a = ratings.setdefault(row.participant_a_id, _INITIAL_RATING)
            b = ratings.setdefault(row.participant_b_id, _INITIAL_RATING)
            ea = Decimal(1) / (Decimal(1) + Decimal(10) ** ((b - a) / Decimal(400)))
            eb = Decimal(1) - ea
            sa, sb = self._expected(row.result)
            ratings[row.participant_a_id] = _round(a + k * (sa - ea))
            ratings[row.participant_b_id] = _round(b + k * (sb - eb))
            support[row.participant_a_id] = support.get(row.participant_a_id, 0) + 1
            support[row.participant_b_id] = support.get(row.participant_b_id, 0) + 1
            last_available[row.participant_a_id] = max(
                last_available.get(row.participant_a_id, datetime.min.replace(tzinfo=timezone.utc)),
                _instant("available_at", row.available_at),
            )
            last_available[row.participant_b_id] = max(
                last_available.get(row.participant_b_id, datetime.min.replace(tzinfo=timezone.utc)),
                _instant("available_at", row.available_at),
            )

        features = []
        for participant_id in sorted(ratings):
            age_days = max(0, (cutoff - last_available[participant_id]).days)
            effective = max(1, support[participant_id])
            uncertainty = _round(
                Decimal(200) / Decimal(effective).sqrt()
                + Decimal(age_days) * Decimal("0.5")
            )
            features.append(
                FeatureSnapshot(
                    participant_id=participant_id,
                    rating=ratings[participant_id],
                    uncertainty=uncertainty,
                    support=support[participant_id],
                    as_of=as_of,
                    view=view,
                )
            )
        input_ids = tuple(sorted(row.observation_id for row in rows))
        input_set_digest = _digest(list(input_ids))
        return RatingSnapshot(
            algorithm=_RATING_ALGORITHM,
            algorithm_version=_RATING_VERSION,
            config_digest=config_digest,
            as_of=as_of,
            view=view,
            input_observation_ids=input_ids,
            input_set_digest=input_set_digest,
            features=tuple(features),
            causal_cutoff=as_of,
        )

    def publish_snapshot(self, snapshot: RatingSnapshot) -> str:
        if not isinstance(snapshot, RatingSnapshot):
            raise TypeError("snapshot must be RatingSnapshot")
        for observation_id in snapshot.input_observation_ids:
            if observation_id not in self._observations:
                raise OpponentGraphError("snapshot references observation not durably committed")
        expected_digest = _digest(list(snapshot.input_observation_ids))
        if snapshot.input_set_digest != expected_digest:
            raise OpponentGraphError("snapshot input set digest mismatch")
        existing = self._snapshots.get(snapshot.snapshot_id)
        if existing is not None:
            if existing != snapshot:
                raise OpponentGraphError("conflicting immutable snapshot identity")
            return snapshot.snapshot_id
        self._snapshots[snapshot.snapshot_id] = snapshot
        self._persist()
        return snapshot.snapshot_id

    def invalidate_for_correction(
        self,
        predecessor_observation_id: str,
        correction_observation_id: str,
    ) -> tuple[str, ...]:
        _sha("predecessor_observation_id", predecessor_observation_id)
        _sha("correction_observation_id", correction_observation_id)
        affected = []
        for snapshot_id, snapshot in tuple(self._snapshots.items()):
            if predecessor_observation_id not in snapshot.input_observation_ids:
                continue
            affected_entities = tuple(sorted(feature.participant_id for feature in snapshot.features))
            work = RecomputeWork.create(
                target_snapshot_id=snapshot_id,
                reason_code="OUTCOME_CORRECTION",
                created_from_observation_id=correction_observation_id,
                affected_entity_ids=affected_entities,
            )
            self._work[work.work_id] = work
            replacement = RatingSnapshot(
                algorithm=snapshot.algorithm,
                algorithm_version=snapshot.algorithm_version,
                config_digest=snapshot.config_digest,
                as_of=snapshot.as_of,
                view=snapshot.view,
                input_observation_ids=snapshot.input_observation_ids,
                input_set_digest=snapshot.input_set_digest,
                features=snapshot.features,
                causal_cutoff=snapshot.causal_cutoff,
                invalidated=True,
                invalidation_work_id=work.work_id,
            )
            self._snapshots.pop(snapshot_id)
            self._snapshots[replacement.snapshot_id] = replacement
            affected.append(replacement.snapshot_id)
        if affected:
            self._persist()
        return tuple(sorted(affected))

    def invalidate_for_identity(
        self,
        entity_id: str,
        *,
        reason_code: str = "IDENTITY_CORRECTION",
    ) -> tuple[str, ...]:
        self._require_entity(entity_id)
        affected = []
        for snapshot_id, snapshot in tuple(self._snapshots.items()):
            if entity_id not in {feature.participant_id for feature in snapshot.features}:
                continue
            work = RecomputeWork.create(
                target_snapshot_id=snapshot_id,
                reason_code=reason_code,
                created_from_observation_id=snapshot.input_observation_ids[0],
                affected_entity_ids=tuple(sorted(feature.participant_id for feature in snapshot.features)),
            )
            self._work[work.work_id] = work
            replacement = RatingSnapshot(
                algorithm=snapshot.algorithm,
                algorithm_version=snapshot.algorithm_version,
                config_digest=snapshot.config_digest,
                as_of=snapshot.as_of,
                view=snapshot.view,
                input_observation_ids=snapshot.input_observation_ids,
                input_set_digest=snapshot.input_set_digest,
                features=snapshot.features,
                causal_cutoff=snapshot.causal_cutoff,
                invalidated=True,
                invalidation_work_id=work.work_id,
            )
            self._snapshots.pop(snapshot_id)
            self._snapshots[replacement.snapshot_id] = replacement
            affected.append(replacement.snapshot_id)
        if affected:
            self._persist()
        return tuple(sorted(affected))

    def observations(self) -> tuple[PerformanceOutcome, ...]:
        return tuple(sorted(self._observations.values(), key=lambda row: row.observation_id))

    def edges(self) -> tuple[OpponentEdge, ...]:
        return tuple(sorted(self._edges.values(), key=lambda row: row.edge_id))

    def snapshots(self) -> tuple[RatingSnapshot, ...]:
        return tuple(sorted(self._snapshots.values(), key=lambda row: row.snapshot_id))

    def recompute_work(self) -> tuple[RecomputeWork, ...]:
        return tuple(sorted(self._work.values(), key=lambda row: row.work_id))

    def _persist(self) -> None:
        payload = {
            "schema": _SCHEMA,
            "version": _VERSION,
            "observations": [
                row.payload() for row in sorted(self._observations.values(), key=lambda row: row.observation_id)
            ],
            "edges": [
                {
                    "observation_id": row.observation_id,
                    "participant_a_id": row.participant_a_id,
                    "participant_b_id": row.participant_b_id,
                    "event_id": row.event_id,
                    "sport_id": row.sport_id,
                    "observed_at": row.observed_at,
                    "available_at": row.available_at,
                    "result": row.result.value,
                    "evidence_sha256": row.evidence_sha256,
                }
                for row in sorted(self._edges.values(), key=lambda row: row.edge_id)
            ],
            "snapshots": [row.payload() for row in sorted(self._snapshots.values(), key=lambda row: row.snapshot_id)],
            "recompute_work": [row.payload() for row in sorted(self._work.values(), key=lambda row: row.work_id)],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with WorkspaceEconomicLock(self.path.parent):
            atomic_write_json(self.path, payload)

    def _persist_loading(self) -> None:
        self._persist()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OpponentGraphError(f"cannot load opponent graph store: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA or raw.get("version") != _VERSION:
            raise OpponentGraphError("unsupported opponent graph store schema")
        self._loading = True
        try:
            for item in raw.get("observations", []):
                obs = PerformanceOutcome(
                    event_id=item["event_id"],
                    sport_id=item["sport_id"],
                    participant_a_id=item["participant_a_id"],
                    participant_b_id=item["participant_b_id"],
                    result=MatchResult(item["result"]),
                    observed_at=item["observed_at"],
                    available_at=item["available_at"],
                    evidence_sha256=item["evidence_sha256"],
                    league_id=item.get("league_id"),
                    market_id=item.get("market_id"),
                    supersedes_observation_id=item.get("supersedes_observation_id"),
                )
                self._observations[obs.observation_id] = obs
                edge = OpponentEdge(
                    observation_id=obs.observation_id,
                    participant_a_id=obs.participant_a_id,
                    participant_b_id=obs.participant_b_id,
                    event_id=obs.event_id,
                    sport_id=obs.sport_id,
                    observed_at=obs.observed_at,
                    available_at=obs.available_at,
                    result=obs.result,
                    evidence_sha256=obs.evidence_sha256,
                )
                self._edges[edge.edge_id] = edge

            by_id = self._observations
            for item in raw.get("snapshots", []):
                features = tuple(
                    FeatureSnapshot(
                        participant_id=f["participant_id"],
                        rating=Decimal(f["rating"]),
                        uncertainty=Decimal(f["uncertainty"]),
                        support=f["support"],
                        as_of=f["as_of"],
                        view=IdentityView(f["view"]),
                    )
                    for f in item["features"]
                )
                snapshot = RatingSnapshot(
                    algorithm=item["algorithm"],
                    algorithm_version=item["algorithm_version"],
                    config_digest=item["config_digest"],
                    as_of=item["as_of"],
                    view=IdentityView(item["view"]),
                    input_observation_ids=tuple(item["input_observation_ids"]),
                    input_set_digest=item["input_set_digest"],
                    features=features,
                    causal_cutoff=item["causal_cutoff"],
                    invalidated=item.get("invalidated", False),
                    invalidation_work_id=item.get("invalidation_work_id"),
                )
                for observation_id in snapshot.input_observation_ids:
                    if observation_id not in by_id:
                        raise OpponentGraphError("stored snapshot references missing observation")
                self._snapshots[snapshot.snapshot_id] = snapshot

            for item in raw.get("recompute_work", []):
                work = RecomputeWork(
                    target_snapshot_id=item["target_snapshot_id"],
                    reason_code=item["reason_code"],
                    status=RecomputeStatus(item["status"]),
                    created_from_observation_id=item["created_from_observation_id"],
                    affected_entity_ids=tuple(item["affected_entity_ids"]),
                    work_id=item["work_id"],
                )
                self._work[work.work_id] = work
        finally:
            self._loading = False
