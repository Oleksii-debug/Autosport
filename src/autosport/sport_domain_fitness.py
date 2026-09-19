"""Causal sport-domain fitness evidence and bounded compute-routing guidance."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .integrity import atomic_write_json

_SCHEMA = "autosport.sport_domain_fitness"
_VERSION = 1
_ZERO = Decimal("0")
_ONE = Decimal("1")


class SportDomainFitnessError(ValueError):
    """Raised when fitness evidence or routing state is non-canonical."""


class EvidenceState(StrEnum):
    MEASURED = "MEASURED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    INSUFFICIENT = "INSUFFICIENT"


class EvidenceProvenance(StrEnum):
    OBSERVED = "OBSERVED"
    SIMULATED = "SIMULATED"


class DomainProfile(StrEnum):
    FAST = "FAST"
    SLOW = "SLOW"


class CausalView(StrEnum):
    AS_KNOWN_AT_DECISION = "AS_KNOWN_AT_DECISION"
    RESTATED_RESEARCH = "RESTATED_RESEARCH"


class RouteStatus(StrEnum):
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    ROUTE_BASELINE = "ROUTE_BASELINE"
    ROUTE_SLOW_RESEARCH = "ROUTE_SLOW_RESEARCH"
    DO_NOT_ROUTE = "DO_NOT_ROUTE"


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise SportDomainFitnessError(f"{name} must be a non-empty canonical string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SportDomainFitnessError(f"{name} must be valid UTF-8") from exc
    return value


def _instant(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SportDomainFitnessError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SportDomainFitnessError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _time(name: str, value: object) -> str:
    return _instant(name, value).isoformat().replace("+00:00", "Z")


def _decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise SportDomainFitnessError(f"{name} must be a finite Decimal")
    return value


def _nonnegative(name: str, value: object) -> Decimal:
    result = _decimal(name, value)
    if result < _ZERO:
        raise SportDomainFitnessError(f"{name} must be non-negative")
    return result


def _fraction(name: str, value: object) -> Decimal:
    result = _decimal(name, value)
    if result < _ZERO or result > _ONE:
        raise SportDomainFitnessError(f"{name} must be between 0 and 1")
    return result


@dataclass(frozen=True, slots=True)
class MetricEvidence:
    state: EvidenceState
    value: Decimal | None
    unit: str

    def __post_init__(self) -> None:
        if not isinstance(self.state, EvidenceState):
            raise SportDomainFitnessError("metric state must be EvidenceState")
        _text("unit", self.unit)
        if self.state is EvidenceState.MEASURED:
            if self.value is None:
                raise SportDomainFitnessError("MEASURED metric requires a value")
            _nonnegative("metric value", self.value)
        elif self.value is not None:
            raise SportDomainFitnessError("unmeasured metric states must not carry a value")

    def payload(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "value": None if self.value is None else str(_decimal("metric value", self.value)),
            "unit": self.unit,
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> "MetricEvidence":
        value = raw.get("value")
        try:
            parsed = None if value is None else Decimal(value)
        except (InvalidOperation, TypeError) as exc:
            raise SportDomainFitnessError("metric value is not a valid Decimal") from exc
        return cls(EvidenceState(raw["state"]), parsed, raw["unit"])


@dataclass(frozen=True, slots=True)
class SportDomainFitnessObservation:
    observation_id: str
    sport_id: str
    league_id: str
    market_id: str
    provider_id: str
    measured_from: str
    measured_until: str
    available_at: str
    evidence_sha256: str
    provenance: EvidenceProvenance
    domain_profile: DomainProfile
    catalogue_coverage: MetricEvidence
    quote_coverage: MetricEvidence
    recurrence_per_hour: MetricEvidence
    freshness_seconds: MetricEvidence
    reaction_slack_seconds: MetricEvidence
    executable_liquidity: MetricEvidence
    fee_fraction: MetricEvidence
    slippage_fraction: MetricEvidence
    capital_time_hours: MetricEvidence
    data_cost: MetricEvidence
    compute_cost: MetricEvidence
    slow_analysis_deadline_seconds: MetricEvidence
    freshness_ttl_seconds: MetricEvidence

    def __post_init__(self) -> None:
        _text("observation_id", self.observation_id)
        for name in ("sport_id", "league_id", "market_id", "provider_id"):
            _text(name, getattr(self, name))
        start = _instant("measured_from", self.measured_from)
        end = _instant("measured_until", self.measured_until)
        available = _instant("available_at", self.available_at)
        if end < start:
            raise SportDomainFitnessError("measured_until precedes measured_from")
        if available < end:
            raise SportDomainFitnessError("available_at precedes measured_until")
        evidence = _text("evidence_sha256", self.evidence_sha256)
        if len(evidence) != 64 or any(ch not in "0123456789abcdef" for ch in evidence):
            raise SportDomainFitnessError("evidence_sha256 must be SHA-256 hex")
        if not isinstance(self.provenance, EvidenceProvenance):
            raise SportDomainFitnessError("provenance must be EvidenceProvenance")
        if not isinstance(self.domain_profile, DomainProfile):
            raise SportDomainFitnessError("domain_profile must be DomainProfile")
        units = {
            "catalogue_coverage": "fraction",
            "quote_coverage": "fraction",
            "recurrence_per_hour": "events/hour",
            "freshness_seconds": "seconds",
            "reaction_slack_seconds": "seconds",
            "executable_liquidity": "units",
            "fee_fraction": "fraction",
            "slippage_fraction": "fraction",
            "capital_time_hours": "hours",
            "data_cost": "cost",
            "compute_cost": "cost",
            "slow_analysis_deadline_seconds": "seconds",
            "freshness_ttl_seconds": "seconds",
        }
        for name, unit in units.items():
            metric = getattr(self, name)
            if not isinstance(metric, MetricEvidence) or metric.unit != unit:
                raise SportDomainFitnessError(f"{name} must use unit {unit!r}")
        for name in ("catalogue_coverage", "quote_coverage", "fee_fraction", "slippage_fraction"):
            metric = getattr(self, name)
            if metric.state is EvidenceState.MEASURED:
                _fraction(name, metric.value)
        for name in (
            "recurrence_per_hour", "freshness_seconds", "reaction_slack_seconds",
            "executable_liquidity", "capital_time_hours", "data_cost", "compute_cost",
            "slow_analysis_deadline_seconds", "freshness_ttl_seconds",
        ):
            metric = getattr(self, name)
            if metric.state is EvidenceState.MEASURED:
                _nonnegative(name, metric.value)
        if self.freshness_ttl_seconds.state is EvidenceState.MEASURED:
            if self.freshness_ttl_seconds.value == _ZERO:
                raise SportDomainFitnessError("freshness_ttl_seconds must be positive")

    def content_payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sport_id": self.sport_id, "league_id": self.league_id,
            "market_id": self.market_id, "provider_id": self.provider_id,
            "measured_from": _time("measured_from", self.measured_from),
            "measured_until": _time("measured_until", self.measured_until),
            "available_at": _time("available_at", self.available_at),
            "evidence_sha256": self.evidence_sha256,
            "provenance": self.provenance.value,
            "domain_profile": self.domain_profile.value,
        }
        for name in (
            "catalogue_coverage", "quote_coverage", "recurrence_per_hour", "freshness_seconds",
            "reaction_slack_seconds", "executable_liquidity", "fee_fraction", "slippage_fraction",
            "capital_time_hours", "data_cost", "compute_cost",
            "slow_analysis_deadline_seconds", "freshness_ttl_seconds",
        ):
            out[name] = getattr(self, name).payload()
        return out

    def payload(self) -> dict[str, Any]:
        return {"observation_id": self.observation_id, **self.content_payload()}

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> "SportDomainFitnessObservation":
        names = (
            "catalogue_coverage", "quote_coverage", "recurrence_per_hour", "freshness_seconds",
            "reaction_slack_seconds", "executable_liquidity", "fee_fraction", "slippage_fraction",
            "capital_time_hours", "data_cost", "compute_cost",
            "slow_analysis_deadline_seconds", "freshness_ttl_seconds",
        )
        values = dict(raw)
        for name in names:
            values[name] = MetricEvidence.from_payload(values[name])
        values["provenance"] = EvidenceProvenance(values["provenance"])
        values["domain_profile"] = DomainProfile(values["domain_profile"])
        return cls(**values)


@dataclass(frozen=True, slots=True)
class RouteRecommendation:
    status: RouteStatus
    reason: str
    observation_id: str
    domain_profile: DomainProfile

    def __post_init__(self) -> None:
        _text("reason", self.reason)


_REQUIRED = (
    "catalogue_coverage", "quote_coverage", "recurrence_per_hour", "freshness_seconds",
    "reaction_slack_seconds", "executable_liquidity", "fee_fraction", "slippage_fraction",
    "capital_time_hours", "data_cost", "compute_cost",
    "slow_analysis_deadline_seconds", "freshness_ttl_seconds",
)


def recommend_route(
    observation: SportDomainFitnessObservation,
    *,
    as_of: str,
    view: CausalView = CausalView.AS_KNOWN_AT_DECISION,
) -> RouteRecommendation:
    boundary = _instant("as_of", as_of)
    if view is CausalView.RESTATED_RESEARCH:
        return RouteRecommendation(
            RouteStatus.DO_NOT_ROUTE,
            "RESTATED_RESEARCH evidence is retrospective and cannot authorize live routing",
            observation.observation_id, observation.domain_profile,
        )
    if view is not CausalView.AS_KNOWN_AT_DECISION:
        raise SportDomainFitnessError("view must be CausalView")
    if observation.provenance is EvidenceProvenance.SIMULATED:
        return RouteRecommendation(
            RouteStatus.DO_NOT_ROUTE,
            "simulated evidence cannot establish historical sport-domain fitness",
            observation.observation_id, observation.domain_profile,
        )
    if boundary < _instant("available_at", observation.available_at) or boundary < _instant("measured_until", observation.measured_until):
        return RouteRecommendation(
            RouteStatus.DO_NOT_ROUTE,
            "evidence is not causally available at the decision boundary",
            observation.observation_id, observation.domain_profile,
        )
    if (
        observation.freshness_seconds.state is EvidenceState.MEASURED
        and observation.freshness_ttl_seconds.state is EvidenceState.MEASURED
        and observation.freshness_seconds.value > observation.freshness_ttl_seconds.value
    ):
        return RouteRecommendation(
            RouteStatus.DO_NOT_ROUTE,
            "measured freshness exceeds the declared evidence TTL",
            observation.observation_id, observation.domain_profile,
        )
    missing = [name for name in _REQUIRED if getattr(observation, name).state is not EvidenceState.MEASURED]
    if missing:
        return RouteRecommendation(
            RouteStatus.INSUFFICIENT_EVIDENCE,
            "required metric evidence is not fully measured: " + ",".join(missing),
            observation.observation_id, observation.domain_profile,
        )
    if observation.catalogue_coverage.value == _ZERO or observation.quote_coverage.value == _ZERO:
        return RouteRecommendation(
            RouteStatus.DO_NOT_ROUTE,
            "measured catalogue or quote coverage is zero",
            observation.observation_id, observation.domain_profile,
        )
    slack = observation.reaction_slack_seconds.value
    compute = observation.compute_cost.value
    deadline = observation.slow_analysis_deadline_seconds.value
    if slack <= _ZERO:
        return RouteRecommendation(
            RouteStatus.DO_NOT_ROUTE, "measured reaction slack is non-positive",
            observation.observation_id, observation.domain_profile,
        )
    if observation.domain_profile is DomainProfile.SLOW:
        if compute + deadline <= slack:
            return RouteRecommendation(
                RouteStatus.ROUTE_SLOW_RESEARCH,
                "measured reaction slack covers measured slow-analysis compute cost plus deadline",
                observation.observation_id, observation.domain_profile,
            )
        return RouteRecommendation(
            RouteStatus.ROUTE_BASELINE,
            "slow-analysis budget does not fit measured reaction slack; stay on baseline route",
            observation.observation_id, observation.domain_profile,
        )
    if compute <= slack:
        return RouteRecommendation(
            RouteStatus.ROUTE_BASELINE,
            "measured reaction slack covers compute cost for the FAST hypothesis route",
            observation.observation_id, observation.domain_profile,
        )
    return RouteRecommendation(
        RouteStatus.DO_NOT_ROUTE,
        "measured reaction slack is insufficient for the required compute cost",
        observation.observation_id, observation.domain_profile,
    )


class SportDomainFitnessStore:
    """Append-only atomic observation store with causal lookup."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._observations: dict[str, SportDomainFitnessObservation] = {}
        if self.path.exists():
            self._load()
        else:
            self._persist(self._observations)

    def _persist(self, staged: Mapping[str, SportDomainFitnessObservation]) -> None:
        atomic_write_json(
            self.path,
            {
                "schema": _SCHEMA,
                "version": _VERSION,
                "observations": [item.payload() for item in sorted(staged.values(), key=lambda x: x.observation_id)],
            },
        )

    def add(self, observation: SportDomainFitnessObservation) -> bool:
        if not isinstance(observation, SportDomainFitnessObservation):
            raise TypeError("observation must be SportDomainFitnessObservation")
        existing = self._observations.get(observation.observation_id)
        if existing is not None:
            if existing != observation:
                raise SportDomainFitnessError("immutable observation id conflicts with stored evidence")
            return False
        staged = dict(self._observations)
        staged[observation.observation_id] = observation
        self._persist(staged)
        self._observations = staged
        return True

    def get(self, observation_id: str) -> SportDomainFitnessObservation | None:
        _text("observation_id", observation_id)
        return self._observations.get(observation_id)

    def lookup(
        self, *, sport_id: str, league_id: str, market_id: str, provider_id: str,
        as_of: str, view: CausalView = CausalView.AS_KNOWN_AT_DECISION,
    ) -> tuple[SportDomainFitnessObservation, ...]:
        boundary = _instant("as_of", as_of)
        if not isinstance(view, CausalView):
            raise SportDomainFitnessError("view must be CausalView")
        ids = tuple(
            _text(name, value)
            for name, value in (
                ("sport_id", sport_id), ("league_id", league_id),
                ("market_id", market_id), ("provider_id", provider_id),
            )
        )
        matches = [
            item for item in self._observations.values()
            if (item.sport_id, item.league_id, item.market_id, item.provider_id) == ids
            and _instant("available_at", item.available_at) <= boundary
            and (view is CausalView.RESTATED_RESEARCH or _instant("measured_until", item.measured_until) <= boundary)
        ]
        return tuple(sorted(matches, key=lambda item: (_instant("available_at", item.available_at), item.observation_id)))

    def recommend(self, **kwargs: Any) -> tuple[RouteRecommendation, ...]:
        return tuple(recommend_route(item, as_of=kwargs["as_of"], view=kwargs.get("view", CausalView.AS_KNOWN_AT_DECISION))
                       for item in self.lookup(**kwargs))
    
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SportDomainFitnessError("cannot load fitness evidence store") from exc
        if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA or raw.get("version") != _VERSION:
            raise SportDomainFitnessError("unsupported fitness evidence store schema")
        values: dict[str, SportDomainFitnessObservation] = {}
        for item in raw.get("observations", []):
            try:
                observation = SportDomainFitnessObservation.from_payload(item)
            except (KeyError, TypeError, ValueError) as exc:
                raise SportDomainFitnessError("invalid persisted fitness observation") from exc
            if observation.observation_id in values:
                raise SportDomainFitnessError("duplicate observation id")
            values[observation.observation_id] = observation
        self._observations = values
