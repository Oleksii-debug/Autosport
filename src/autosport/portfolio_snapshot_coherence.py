"""Fail-closed coherence proof for portfolio inputs observed around one decision cut.

This module is deliberately non-authoritative.  It does not calculate portfolio
exposure, P&L, risk, stake, liquidity, execution eligibility, or any money-moving
state.  It only answers whether an exact set of externally produced component
evidence is temporally coherent enough to be handed to an existing portfolio
scenario calculation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Final, Iterable


_SCHEMA: Final = "autosport.portfolio_snapshot_coherence"
_SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")


class PortfolioSnapshotCoherenceError(ValueError):
    """The caller supplied malformed or non-canonical evidence/configuration."""


class SnapshotCoherenceStatus(str, Enum):
    """Read-only disposition for one attempted portfolio evidence cut."""

    COHERENT = "COHERENT"
    WAIT_INCOMPLETE = "WAIT_INCOMPLETE"
    WAIT_AMBIGUOUS = "WAIT_AMBIGUOUS"
    WAIT_UNEXPECTED_COMPONENT = "WAIT_UNEXPECTED_COMPONENT"
    WAIT_FUTURE = "WAIT_FUTURE"
    WAIT_STALE = "WAIT_STALE"
    WAIT_MIXED_CUT = "WAIT_MIXED_CUT"


def _canonical_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise PortfolioSnapshotCoherenceError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PortfolioSnapshotCoherenceError(f"{name} must be valid UTF-8") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _canonical_text(value, name)
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise PortfolioSnapshotCoherenceError(f"{name} must be lowercase SHA-256 hex")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _canonical_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PortfolioSnapshotCoherenceError(
            f"{name} must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PortfolioSnapshotCoherenceError(
            f"{name} must be timezone-aware ISO-8601"
        )
    return parsed.astimezone(timezone.utc)


def _instant_id(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise PortfolioSnapshotCoherenceError(f"{name} must be a non-negative integer")
    try:
        timedelta(seconds=value)
    except OverflowError as exc:
        raise PortfolioSnapshotCoherenceError(
            f"{name} must fit the supported duration range"
        ) from exc
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PortfolioSnapshotCoherenceError("payload is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PortfolioComponentEvidence:
    """Identity and causal timing of one externally produced portfolio input.

    ``component_key`` names the semantic input required by the scenario policy.
    The evidence bytes themselves remain owned by their source authority; this
    contract only binds their immutable identity and causal availability time.
    """

    component_key: str
    evidence_id: str
    evidence_sha256: str
    observed_at: str
    available_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "component_key", _canonical_text(self.component_key, "component_key")
        )
        object.__setattr__(
            self, "evidence_id", _canonical_text(self.evidence_id, "evidence_id")
        )
        object.__setattr__(
            self,
            "evidence_sha256",
            _sha256(self.evidence_sha256, "evidence_sha256"),
        )
        observed = _instant(self.observed_at, "observed_at")
        available = _instant(self.available_at, "available_at")
        if available < observed:
            raise PortfolioSnapshotCoherenceError(
                "available_at cannot precede observed_at"
            )
        object.__setattr__(
            self, "observed_at", observed.isoformat().replace("+00:00", "Z")
        )
        object.__setattr__(
            self, "available_at", available.isoformat().replace("+00:00", "Z")
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "component_key": self.component_key,
            "evidence_id": self.evidence_id,
            "evidence_sha256": self.evidence_sha256,
            "observed_at": self.observed_at,
            "available_at": self.available_at,
        }


@dataclass(frozen=True, slots=True)
class _DerivedCoherence:
    decision_as_of: str
    required_components: tuple[str, ...]
    max_age_seconds: int
    max_cut_skew_seconds: int
    components: tuple[PortfolioComponentEvidence, ...]
    status: SnapshotCoherenceStatus
    reasons: tuple[str, ...]
    policy_sha256: str
    component_set_sha256: str
    coherence_id: str


def _derive_snapshot_coherence(
    *,
    decision_as_of: object,
    required_components: Iterable[str],
    components: Iterable[PortfolioComponentEvidence],
    max_age_seconds: object,
    max_cut_skew_seconds: object,
) -> _DerivedCoherence:
    """Derive the only valid immutable representation of one coherence result."""

    as_of = _instant(decision_as_of, "decision_as_of")
    max_age = _nonnegative_int(max_age_seconds, "max_age_seconds")
    max_skew = _nonnegative_int(max_cut_skew_seconds, "max_cut_skew_seconds")

    required_raw = tuple(
        _canonical_text(value, "required_components item") for value in required_components
    )
    if not required_raw:
        raise PortfolioSnapshotCoherenceError("required_components cannot be empty")
    if len(set(required_raw)) != len(required_raw):
        raise PortfolioSnapshotCoherenceError("required_components must be unique")
    required = tuple(sorted(required_raw))

    supplied = tuple(components)
    if any(type(item) is not PortfolioComponentEvidence for item in supplied):
        raise PortfolioSnapshotCoherenceError(
            "components must contain exact PortfolioComponentEvidence values"
        )
    ordered = tuple(
        sorted(
            supplied,
            key=lambda item: (
                item.component_key,
                item.available_at,
                item.observed_at,
                item.evidence_id,
                item.evidence_sha256,
            ),
        )
    )

    policy_payload = {
        "required_components": list(required),
        "max_age_seconds": max_age,
        "max_cut_skew_seconds": max_skew,
    }
    policy_sha256 = _digest(policy_payload)
    component_payload = [item.to_dict() for item in ordered]
    component_set_sha256 = _digest(component_payload)

    grouped: dict[str, list[PortfolioComponentEvidence]] = {}
    for item in ordered:
        grouped.setdefault(item.component_key, []).append(item)

    required_set = set(required)
    supplied_set = set(grouped)
    missing = tuple(sorted(required_set - supplied_set))
    unexpected = tuple(sorted(supplied_set - required_set))
    ambiguous = tuple(
        sorted(key for key in required if len(grouped.get(key, ())) > 1)
    )

    status: SnapshotCoherenceStatus
    reasons: tuple[str, ...]
    selected = tuple(
        grouped[key][0]
        for key in required
        if len(grouped.get(key, ())) == 1
    )

    if missing:
        status = SnapshotCoherenceStatus.WAIT_INCOMPLETE
        reasons = tuple(f"missing:{key}" for key in missing)
    elif unexpected:
        status = SnapshotCoherenceStatus.WAIT_UNEXPECTED_COMPONENT
        reasons = tuple(f"unexpected:{key}" for key in unexpected)
    elif ambiguous:
        status = SnapshotCoherenceStatus.WAIT_AMBIGUOUS
        reasons = tuple(f"ambiguous:{key}" for key in ambiguous)
    else:
        future = tuple(
            item.component_key
            for item in selected
            if _instant(item.available_at, "available_at") > as_of
        )
        if future:
            status = SnapshotCoherenceStatus.WAIT_FUTURE
            reasons = tuple(f"future:{key}" for key in future)
        else:
            stale = tuple(
                item.component_key
                for item in selected
                if as_of - _instant(item.observed_at, "observed_at")
                > timedelta(seconds=max_age)
            )
            if stale:
                status = SnapshotCoherenceStatus.WAIT_STALE
                reasons = tuple(f"stale:{key}" for key in stale)
            else:
                observations = tuple(
                    _instant(item.observed_at, "observed_at") for item in selected
                )
                availability = tuple(
                    _instant(item.available_at, "available_at") for item in selected
                )
                observation_skew = max(observations) - min(observations)
                availability_skew = max(availability) - min(availability)
                cut_skew = max(observation_skew, availability_skew)
                if cut_skew > timedelta(seconds=max_skew):
                    status = SnapshotCoherenceStatus.WAIT_MIXED_CUT
                    skew_microseconds = cut_skew // timedelta(microseconds=1)
                    reasons = (f"cut_skew_microseconds:{skew_microseconds}",)
                else:
                    status = SnapshotCoherenceStatus.COHERENT
                    reasons = ()

    decision_id = as_of.isoformat().replace("+00:00", "Z")
    identity_payload = {
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "status": status.value,
        "decision_as_of": decision_id,
        "policy_sha256": policy_sha256,
        "component_set_sha256": component_set_sha256,
        "reasons": list(reasons),
    }
    coherence_id = _digest(identity_payload)
    return _DerivedCoherence(
        decision_as_of=decision_id,
        required_components=required,
        max_age_seconds=max_age,
        max_cut_skew_seconds=max_skew,
        components=ordered,
        status=status,
        reasons=reasons,
        policy_sha256=policy_sha256,
        component_set_sha256=component_set_sha256,
        coherence_id=coherence_id,
    )


@dataclass(frozen=True, slots=True)
class PortfolioSnapshotCoherence:
    """Deterministic read-only result for one evidence cut.

    Direct construction is allowed only when every stored field is exactly equal
    to the canonical derivation from the stored decision cut, policy and evidence.
    This prevents callers/deserializers from minting a positive ``COHERENT`` result.
    """

    status: SnapshotCoherenceStatus
    decision_as_of: str
    required_components: tuple[str, ...]
    max_age_seconds: int
    max_cut_skew_seconds: int
    components: tuple[PortfolioComponentEvidence, ...]
    reasons: tuple[str, ...]
    policy_sha256: str
    component_set_sha256: str
    coherence_id: str

    def __post_init__(self) -> None:
        derived = _derive_snapshot_coherence(
            decision_as_of=self.decision_as_of,
            required_components=self.required_components,
            components=self.components,
            max_age_seconds=self.max_age_seconds,
            max_cut_skew_seconds=self.max_cut_skew_seconds,
        )
        exact_fields = (
            ("status", self.status, derived.status),
            ("decision_as_of", self.decision_as_of, derived.decision_as_of),
            ("required_components", self.required_components, derived.required_components),
            ("max_age_seconds", self.max_age_seconds, derived.max_age_seconds),
            (
                "max_cut_skew_seconds",
                self.max_cut_skew_seconds,
                derived.max_cut_skew_seconds,
            ),
            ("components", self.components, derived.components),
            ("reasons", self.reasons, derived.reasons),
            ("policy_sha256", self.policy_sha256, derived.policy_sha256),
            (
                "component_set_sha256",
                self.component_set_sha256,
                derived.component_set_sha256,
            ),
            ("coherence_id", self.coherence_id, derived.coherence_id),
        )
        for name, actual, expected in exact_fields:
            if type(actual) is not type(expected) or actual != expected:
                raise PortfolioSnapshotCoherenceError(
                    f"{name} must match deterministic coherence derivation"
                )

    @property
    def coherent(self) -> bool:
        """Whether this cut is temporally coherent; this grants no action authority."""

        return self.status is SnapshotCoherenceStatus.COHERENT

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "status": self.status.value,
            "decision_as_of": self.decision_as_of,
            "required_components": list(self.required_components),
            "max_age_seconds": self.max_age_seconds,
            "max_cut_skew_seconds": self.max_cut_skew_seconds,
            "components": [item.to_dict() for item in self.components],
            "reasons": list(self.reasons),
            "policy_sha256": self.policy_sha256,
            "component_set_sha256": self.component_set_sha256,
            "coherence_id": self.coherence_id,
            "portfolio_calculation_authority": False,
            "risk_authority": False,
            "execution_authority": False,
            "real_money_execution": False,
        }


def evaluate_portfolio_snapshot_coherence(
    *,
    decision_as_of: str,
    required_components: Iterable[str],
    components: Iterable[PortfolioComponentEvidence],
    max_age_seconds: int,
    max_cut_skew_seconds: int,
) -> PortfolioSnapshotCoherence:
    """Evaluate one exact causal cut without deriving portfolio economics.

    Status precedence is intentionally deterministic and fail-closed:
    incomplete -> unexpected -> ambiguous -> future -> stale -> mixed-cut -> coherent.
    Input order never changes identity or disposition.
    """

    derived = _derive_snapshot_coherence(
        decision_as_of=decision_as_of,
        required_components=required_components,
        components=components,
        max_age_seconds=max_age_seconds,
        max_cut_skew_seconds=max_cut_skew_seconds,
    )
    return PortfolioSnapshotCoherence(
        status=derived.status,
        decision_as_of=derived.decision_as_of,
        required_components=derived.required_components,
        max_age_seconds=derived.max_age_seconds,
        max_cut_skew_seconds=derived.max_cut_skew_seconds,
        components=derived.components,
        reasons=derived.reasons,
        policy_sha256=derived.policy_sha256,
        component_set_sha256=derived.component_set_sha256,
        coherence_id=derived.coherence_id,
    )
