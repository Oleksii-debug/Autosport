"""Deterministic causal multi-sport capability recommendation projection.

This module composes the existing sport-domain fitness authority. It does not
create provider evidence, economic/risk authority, execution permission, model
promotion authority, or real-money permission.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Iterable

from .sport_domain_fitness import (
    CausalView,
    RouteStatus,
    SportDomainFitnessObservation,
    recommend_route,
)


_MAX_TARGETS = 256
_MAX_OBSERVATIONS = 4096


class MultiSportCapabilityError(ValueError):
    """Raised when a multi-sport capability projection is non-canonical."""


class CapabilityRecommendationStatus(StrEnum):
    RECOMMEND_BASELINE = "RECOMMEND_BASELINE"
    RECOMMEND_SLOW_RESEARCH = "RECOMMEND_SLOW_RESEARCH"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    BLOCKED = "BLOCKED"


def _text(name: str, value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise MultiSportCapabilityError(
            f"{name} must be a non-empty canonical string"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise MultiSportCapabilityError(
            f"{name} must be valid UTF-8"
        ) from exc
    return value


def _instant(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MultiSportCapabilityError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MultiSportCapabilityError(
            f"{name} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _time(name: str, value: object) -> str:
    return _instant(name, value).isoformat().replace("+00:00", "Z")


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SportCapabilityTarget:
    sport_id: str
    league_id: str
    market_id: str
    provider_id: str

    def __post_init__(self) -> None:
        for name in (
            "sport_id",
            "league_id",
            "market_id",
            "provider_id",
        ):
            _text(name, getattr(self, name))

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.sport_id,
            self.league_id,
            self.market_id,
            self.provider_id,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "sport_id": self.sport_id,
            "league_id": self.league_id,
            "market_id": self.market_id,
            "provider_id": self.provider_id,
        }


@dataclass(frozen=True, slots=True)
class SportCapabilityDecision:
    target: SportCapabilityTarget
    status: CapabilityRecommendationStatus
    observation_id: str | None
    evidence_sha256: str | None
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.target, SportCapabilityTarget):
            raise MultiSportCapabilityError(
                "target must be SportCapabilityTarget"
            )
        if not isinstance(self.status, CapabilityRecommendationStatus):
            raise MultiSportCapabilityError(
                "status must be CapabilityRecommendationStatus"
            )
        _text("reason", self.reason)
        if (self.observation_id is None) != (self.evidence_sha256 is None):
            raise MultiSportCapabilityError(
                "observation_id and evidence_sha256 must be present together"
            )
        if self.observation_id is not None:
            _text("observation_id", self.observation_id)
            evidence = _text("evidence_sha256", self.evidence_sha256)
            if len(evidence) != 64 or any(
                ch not in "0123456789abcdef" for ch in evidence
            ):
                raise MultiSportCapabilityError(
                    "evidence_sha256 must be SHA-256 hex"
                )

    @property
    def recommended(self) -> bool:
        return self.status in {
            CapabilityRecommendationStatus.RECOMMEND_BASELINE,
            CapabilityRecommendationStatus.RECOMMEND_SLOW_RESEARCH,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target.to_dict(),
            "status": self.status.value,
            "observation_id": self.observation_id,
            "evidence_sha256": self.evidence_sha256,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class MultiSportCapabilitySnapshot:
    as_of: str
    decisions: tuple[SportCapabilityDecision, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _time("as_of", self.as_of))
        values = tuple(self.decisions)
        if len(values) > _MAX_TARGETS:
            raise MultiSportCapabilityError(
                f"capability snapshot exceeds {_MAX_TARGETS} targets"
            )
        if any(
            not isinstance(item, SportCapabilityDecision)
            for item in values
        ):
            raise MultiSportCapabilityError(
                "decisions must contain SportCapabilityDecision values"
            )
        ordered = tuple(sorted(values, key=lambda item: item.target.key))
        keys = [item.target.key for item in ordered]
        if len(set(keys)) != len(keys):
            raise MultiSportCapabilityError(
                "capability snapshot contains duplicate targets"
            )
        object.__setattr__(self, "decisions", ordered)

    @property
    def snapshot_id(self) -> str:
        return _digest(
            {
                "as_of": self.as_of,
                "decisions": [item.to_dict() for item in self.decisions],
            }
        )

    @property
    def recommended_targets(self) -> tuple[SportCapabilityTarget, ...]:
        return tuple(
            item.target for item in self.decisions if item.recommended
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of,
            "decisions": [item.to_dict() for item in self.decisions],
        }


_ROUTE_MAP = {
    RouteStatus.ROUTE_BASELINE:
        CapabilityRecommendationStatus.RECOMMEND_BASELINE,
    RouteStatus.ROUTE_SLOW_RESEARCH:
        CapabilityRecommendationStatus.RECOMMEND_SLOW_RESEARCH,
    RouteStatus.INSUFFICIENT_EVIDENCE:
        CapabilityRecommendationStatus.INSUFFICIENT_EVIDENCE,
    RouteStatus.DO_NOT_ROUTE:
        CapabilityRecommendationStatus.BLOCKED,
}


def project_multisport_capability(
    targets: Iterable[SportCapabilityTarget],
    observations: Iterable[SportDomainFitnessObservation],
    *,
    as_of: str,
) -> MultiSportCapabilitySnapshot:
    """Project exact requested sport domains from causal fitness evidence.

    Only evidence that was both measured and available at as_of participates.
    For each exact target, the latest causal observation controls the recommendation.
    Multiple observations at the same latest causal boundary are ambiguous and
    block the recommendation rather than depending on caller iteration order.

    The result is advisory capability/routing recommendation evidence only.
    Positive recommendation states are not product admission or proof that the
    caller-supplied observation has product-owned measurement authority. The
    result never authorizes stakes, provider writes, execution, model promotion,
    or real-money activity.
    """

    boundary = _instant("as_of", as_of)
    canonical_as_of = boundary.isoformat().replace("+00:00", "Z")

    requested = tuple(targets)
    if len(requested) > _MAX_TARGETS:
        raise MultiSportCapabilityError(
            f"target count exceeds {_MAX_TARGETS}"
        )
    if any(not isinstance(item, SportCapabilityTarget) for item in requested):
        raise MultiSportCapabilityError(
            "targets must contain SportCapabilityTarget values"
        )
    keys = [item.key for item in requested]
    if len(set(keys)) != len(keys):
        raise MultiSportCapabilityError(
            "requested capability targets contain duplicates"
        )

    evidence = tuple(observations)
    if len(evidence) > _MAX_OBSERVATIONS:
        raise MultiSportCapabilityError(
            f"observation count exceeds {_MAX_OBSERVATIONS}"
        )
    if any(
        not isinstance(item, SportDomainFitnessObservation)
        for item in evidence
    ):
        raise MultiSportCapabilityError(
            "observations must contain SportDomainFitnessObservation values"
        )

    decisions: list[SportCapabilityDecision] = []
    for target in requested:
        causal = [
            item
            for item in evidence
            if (
                item.sport_id,
                item.league_id,
                item.market_id,
                item.provider_id,
            ) == target.key
            and _instant("available_at", item.available_at) <= boundary
            and _instant("measured_until", item.measured_until) <= boundary
        ]
        if not causal:
            decisions.append(
                SportCapabilityDecision(
                    target=target,
                    status=CapabilityRecommendationStatus.INSUFFICIENT_EVIDENCE,
                    observation_id=None,
                    evidence_sha256=None,
                    reason=(
                        "no causally available sport-domain fitness "
                        "observation exists for the exact requested target"
                    ),
                )
            )
            continue

        latest_available = max(
            _instant("available_at", item.available_at)
            for item in causal
        )
        latest_by_availability = [
            item
            for item in causal
            if _instant("available_at", item.available_at)
            == latest_available
        ]
        latest_measured = max(
            _instant("measured_until", item.measured_until)
            for item in latest_by_availability
        )
        latest = [
            item
            for item in latest_by_availability
            if _instant("measured_until", item.measured_until)
            == latest_measured
        ]
        if len(latest) != 1:
            decisions.append(
                SportCapabilityDecision(
                    target=target,
                    status=CapabilityRecommendationStatus.BLOCKED,
                    observation_id=None,
                    evidence_sha256=None,
                    reason=(
                        "multiple equally-current sport-domain fitness "
                        "observations make the requested target ambiguous"
                    ),
                )
            )
            continue

        observation = latest[0]
        route = recommend_route(
            observation,
            as_of=canonical_as_of,
            view=CausalView.AS_KNOWN_AT_DECISION,
        )
        try:
            status = _ROUTE_MAP[route.status]
        except KeyError as exc:
            raise MultiSportCapabilityError(
                "sport-domain route status is unsupported"
            ) from exc

        decisions.append(
            SportCapabilityDecision(
                target=target,
                status=status,
                observation_id=observation.observation_id,
                evidence_sha256=observation.evidence_sha256,
                reason=route.reason,
            )
        )

    return MultiSportCapabilitySnapshot(
        as_of=canonical_as_of,
        decisions=tuple(decisions),
    )
