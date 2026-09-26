from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable


class LivePortfolioScenarioError(ValueError):
    """Raised when a live portfolio scenario vector is not structurally coherent."""


def _text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise LivePortfolioScenarioError(
            f"{name} must be non-empty canonical text"
        )
    return value


def _sha256(value: object, name: str) -> str:
    raw = _text(value, name)
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
        raise LivePortfolioScenarioError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return raw


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise LivePortfolioScenarioError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise LivePortfolioScenarioError(
            f"{name} must be a non-negative integer"
        )
    return value


def _money(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise LivePortfolioScenarioError(
            f"{name} must be a finite non-negative Decimal"
        )
    return value


def _time(value: object, name: str) -> datetime:
    raw = _text(value, name)
    if re.search(r"[.,]\d{7,}", raw):
        raise LivePortfolioScenarioError(
            f"{name} must not exceed microsecond precision"
        )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LivePortfolioScenarioError(
            f"{name} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LivePortfolioScenarioError(
            f"{name} must be timezone-aware"
        )
    return parsed


def _utc_instant(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _utc_text(value: str) -> str:
    parsed = _time(value, "timestamp")
    return (
        _utc_instant(parsed)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _delta_microseconds(later: datetime, earlier: datetime) -> int:
    delta = later.astimezone(timezone.utc) - earlier.astimezone(timezone.utc)
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _decimal_parts(value: Decimal) -> tuple[int, int]:
    """Return exact signed coefficient and base-10 exponent, context-independently."""
    sign, digits, exponent = value.as_tuple()
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    if sign:
        coefficient = -coefficient
    return coefficient, exponent


def _exact_sum(values: Iterable[Decimal]) -> Decimal:
    """Sum finite Decimal values without consulting the ambient Decimal context."""
    parts = [_decimal_parts(value) for value in values]
    if not parts:
        return Decimal(0)
    common_exponent = min(exponent for _, exponent in parts)
    coefficient = sum(
        coefficient * (10 ** (exponent - common_exponent))
        for coefficient, exponent in parts
    )
    sign = 1 if coefficient < 0 else 0
    digits_text = str(abs(coefficient))
    digits = tuple(int(char) for char in digits_text)
    return Decimal((sign, digits, common_exponent))


def _canonical_decimal(value: Decimal) -> str:
    """Canonical exact decimal text, independent of scale and ambient context."""
    coefficient, exponent = _decimal_parts(value)
    if coefficient == 0:
        return "0"
    negative = coefficient < 0
    coefficient = abs(coefficient)
    while coefficient % 10 == 0:
        coefficient //= 10
        exponent += 1
    digits = str(coefficient)
    if exponent >= 0:
        text = digits + ("0" * exponent)
    else:
        point = len(digits) + exponent
        if point > 0:
            text = digits[:point] + "." + digits[point:]
        else:
            text = "0." + ("0" * (-point)) + digits
    return f"-{text}" if negative else text


def _digest(payload: object) -> str:
    try:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise LivePortfolioScenarioError(
            "scenario evidence is not canonical JSON"
        ) from exc
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class FrozenLiveScenarioSpec:
    """Structural boundary for one portfolio-state live-update vector.

    This object does not prove that ``expected_position_ids`` is the complete
    external/provider universe. It only declares the exact product-side vector
    that this structural check must cover.
    """

    scenario_id: str
    batch_id: str
    portfolio_state_sha256: str
    generation: int
    expected_position_ids: tuple[str, ...]
    batch_opened_at: str
    batch_closed_at: str
    max_observation_skew_microseconds: int

    def __post_init__(self) -> None:
        _text(self.scenario_id, "scenario_id")
        _text(self.batch_id, "batch_id")
        _sha256(self.portfolio_state_sha256, "portfolio_state_sha256")
        _positive_int(self.generation, "generation")
        if type(self.expected_position_ids) is not tuple:
            raise LivePortfolioScenarioError(
                "expected_position_ids must be a tuple"
            )
        for position_id in self.expected_position_ids:
            _text(position_id, "expected_position_id")
        if len(set(self.expected_position_ids)) != len(
            self.expected_position_ids
        ):
            raise LivePortfolioScenarioError(
                "expected_position_ids must be unique"
            )
        opened = _time(self.batch_opened_at, "batch_opened_at")
        closed = _time(self.batch_closed_at, "batch_closed_at")
        if _utc_instant(closed) < _utc_instant(opened):
            raise LivePortfolioScenarioError(
                "batch_closed_at must not precede batch_opened_at"
            )
        _nonnegative_int(
            self.max_observation_skew_microseconds,
            "max_observation_skew_microseconds",
        )


@dataclass(frozen=True, slots=True)
class LivePositionScenarioComponent:
    """One supplied position view within a frozen product scenario vector."""

    position_id: str
    event_id: str
    market_id: str
    provider_id: str
    account_id: str
    batch_id: str
    portfolio_state_sha256: str
    generation: int
    position_state_sha256: str
    observed_at: str
    committed_at: str
    capital_at_risk: Decimal
    conservative_loss_upper_bound: Decimal

    def __post_init__(self) -> None:
        for name in (
            "position_id",
            "event_id",
            "market_id",
            "provider_id",
            "account_id",
            "batch_id",
        ):
            _text(getattr(self, name), name)
        _sha256(self.portfolio_state_sha256, "portfolio_state_sha256")
        _positive_int(self.generation, "generation")
        _sha256(self.position_state_sha256, "position_state_sha256")
        observed = _time(self.observed_at, "observed_at")
        committed = _time(self.committed_at, "committed_at")
        if _utc_instant(committed) < _utc_instant(observed):
            raise LivePortfolioScenarioError(
                "committed_at must not precede observed_at"
            )
        capital = _money(self.capital_at_risk, "capital_at_risk")
        loss = _money(
            self.conservative_loss_upper_bound,
            "conservative_loss_upper_bound",
        )
        if loss > capital:
            raise LivePortfolioScenarioError(
                "conservative loss bound cannot exceed capital_at_risk"
            )


@dataclass(frozen=True, slots=True)
class LivePortfolioScenarioEvidence:
    scenario_id: str
    batch_id: str
    portfolio_state_sha256: str
    generation: int
    position_count: int
    total_capital_at_risk: Decimal
    conservative_componentwise_loss_upper_bound: Decimal
    observation_skew_microseconds: int
    structural_vector_complete: bool
    source_authority_proven: bool
    portfolio_universe_authoritative: bool
    cross_provider_atomicity_proven: bool
    terminal_outcome_exactness_proven: bool
    grants_sizing_authority: bool
    grants_execution_authority: bool
    grants_settlement_authority: bool
    evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.scenario_id, "scenario_id")
        _text(self.batch_id, "batch_id")
        _sha256(self.portfolio_state_sha256, "portfolio_state_sha256")
        _positive_int(self.generation, "generation")
        _nonnegative_int(self.position_count, "position_count")
        total_capital = _money(
            self.total_capital_at_risk,
            "total_capital_at_risk",
        )
        conservative_loss = _money(
            self.conservative_componentwise_loss_upper_bound,
            "conservative_componentwise_loss_upper_bound",
        )
        if conservative_loss > total_capital:
            raise LivePortfolioScenarioError(
                "conservative componentwise loss cannot exceed total capital at risk"
            )
        _nonnegative_int(
            self.observation_skew_microseconds,
            "observation_skew_microseconds",
        )
        if self.structural_vector_complete is not True:
            raise LivePortfolioScenarioError(
                "structural_vector_complete must be exact True"
            )
        for field_name in (
            "source_authority_proven",
            "portfolio_universe_authoritative",
            "cross_provider_atomicity_proven",
            "terminal_outcome_exactness_proven",
            "grants_sizing_authority",
            "grants_execution_authority",
            "grants_settlement_authority",
        ):
            value = getattr(self, field_name)
            if value is not False:
                raise LivePortfolioScenarioError(
                    f"{field_name} must remain exact False on structural evidence"
                )
        _sha256(self.evidence_sha256, "evidence_sha256")


def evaluate_live_portfolio_scenario(
    spec: FrozenLiveScenarioSpec,
    components: tuple[LivePositionScenarioComponent, ...],
) -> LivePortfolioScenarioEvidence:
    """Validate one complete declared live vector and aggregate conservative risk.

    The function intentionally proves only *structural* consistency relative to
    the supplied ``FrozenLiveScenarioSpec``. It does not prove provider
    acquisition, cross-provider simultaneity, external-universe completeness,
    complete terminal outcome coverage, or permission to size/execute/settle.
    """

    if type(spec) is not FrozenLiveScenarioSpec:
        raise LivePortfolioScenarioError(
            "spec must be an exact FrozenLiveScenarioSpec"
        )
    if type(components) is not tuple or any(
        type(item) is not LivePositionScenarioComponent
        for item in components
    ):
        raise LivePortfolioScenarioError(
            "components must be a tuple of LivePositionScenarioComponent"
        )

    by_position: dict[str, LivePositionScenarioComponent] = {}
    for component in components:
        if component.position_id in by_position:
            raise LivePortfolioScenarioError(
                "scenario contains duplicate position_id"
            )
        by_position[component.position_id] = component

    expected = set(spec.expected_position_ids)
    supplied = set(by_position)
    if supplied != expected:
        missing = sorted(expected - supplied)
        unexpected = sorted(supplied - expected)
        raise LivePortfolioScenarioError(
            "scenario position vector does not exactly cover declared positions: "
            f"missing={missing!r}, unexpected={unexpected!r}"
        )

    opened = _time(spec.batch_opened_at, "batch_opened_at")
    closed = _time(spec.batch_closed_at, "batch_closed_at")
    observations: list[datetime] = []

    for position_id in sorted(by_position):
        component = by_position[position_id]
        if component.batch_id != spec.batch_id:
            raise LivePortfolioScenarioError(
                "component batch_id mismatches frozen scenario"
            )
        if component.portfolio_state_sha256 != spec.portfolio_state_sha256:
            raise LivePortfolioScenarioError(
                "component portfolio state mismatches frozen scenario"
            )
        if component.generation != spec.generation:
            raise LivePortfolioScenarioError(
                "component generation mismatches frozen scenario"
            )

        observed = _time(component.observed_at, "observed_at")
        committed = _time(component.committed_at, "committed_at")
        observed_utc = _utc_instant(observed)
        committed_utc = _utc_instant(committed)
        opened_utc = _utc_instant(opened)
        closed_utc = _utc_instant(closed)
        if observed_utc < opened_utc or observed_utc > closed_utc:
            raise LivePortfolioScenarioError(
                "component observation is outside frozen batch interval"
            )
        if committed_utc > closed_utc:
            raise LivePortfolioScenarioError(
                "component commit is later than frozen batch close"
            )
        observations.append(observed_utc)

    if len(observations) < 2:
        skew_microseconds = 0
    else:
        earliest = min(observations)
        latest = max(observations)
        skew_microseconds = _delta_microseconds(latest, earliest)
    if skew_microseconds > spec.max_observation_skew_microseconds:
        raise LivePortfolioScenarioError(
            "component observation skew exceeds frozen scenario policy"
        )

    ordered = tuple(by_position[position_id] for position_id in sorted(by_position))
    total_capital = _exact_sum(item.capital_at_risk for item in ordered)
    conservative_loss = _exact_sum(
        item.conservative_loss_upper_bound for item in ordered
    )

    payload = {
        "schema": "autosport.portfolio_live_scenario",
        "schema_version": 1,
        "scenario_id": spec.scenario_id,
        "batch_id": spec.batch_id,
        "portfolio_state_sha256": spec.portfolio_state_sha256,
        "generation": spec.generation,
        "expected_position_ids": sorted(spec.expected_position_ids),
        "batch_opened_at": _utc_text(spec.batch_opened_at),
        "batch_closed_at": _utc_text(spec.batch_closed_at),
        "max_observation_skew_microseconds": spec.max_observation_skew_microseconds,
        "positions": [
            {
                "position_id": item.position_id,
                "event_id": item.event_id,
                "market_id": item.market_id,
                "provider_id": item.provider_id,
                "account_id": item.account_id,
                "position_state_sha256": item.position_state_sha256,
                "observed_at": _utc_text(item.observed_at),
                "committed_at": _utc_text(item.committed_at),
                "capital_at_risk": _canonical_decimal(item.capital_at_risk),
                "conservative_loss_upper_bound": _canonical_decimal(
                    item.conservative_loss_upper_bound
                ),
            }
            for item in ordered
        ],
        "total_capital_at_risk": _canonical_decimal(total_capital),
        "conservative_componentwise_loss_upper_bound": _canonical_decimal(
            conservative_loss
        ),
        "observation_skew_microseconds": skew_microseconds,
        "structural_vector_complete": True,
        "source_authority_proven": False,
        "portfolio_universe_authoritative": False,
        "cross_provider_atomicity_proven": False,
        "terminal_outcome_exactness_proven": False,
        "grants_sizing_authority": False,
        "grants_execution_authority": False,
        "grants_settlement_authority": False,
    }
    return LivePortfolioScenarioEvidence(
        scenario_id=spec.scenario_id,
        batch_id=spec.batch_id,
        portfolio_state_sha256=spec.portfolio_state_sha256,
        generation=spec.generation,
        position_count=len(ordered),
        total_capital_at_risk=total_capital,
        conservative_componentwise_loss_upper_bound=conservative_loss,
        observation_skew_microseconds=skew_microseconds,
        structural_vector_complete=True,
        source_authority_proven=False,
        portfolio_universe_authoritative=False,
        cross_provider_atomicity_proven=False,
        terminal_outcome_exactness_proven=False,
        grants_sizing_authority=False,
        grants_execution_authority=False,
        grants_settlement_authority=False,
        evidence_sha256=_digest(payload),
    )
