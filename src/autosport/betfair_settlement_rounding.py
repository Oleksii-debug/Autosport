"""Fail-closed Betfair settlement-rounding economics primitives.

This module deliberately separates a mathematical continuous-money model from a
candidate structural settlement-money model. The ordinary Exchange helper
encodes one candidate two-stage *shape* (nearest-cent gross, then nearest-cent
commission) for bounded uncertainty analysis. Public Betfair rules establish
cent rounding of market winnings/losses and commission charges, but this module
does not claim that those rules establish the commission-base ordering or that
this exact shape applies to an arbitrary market. BSP, dead-heat,
reduction-factor, void/correction and market-specific rule semantics remain
outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from enum import Enum


_MAX_COEFFICIENT_DIGITS = 256
_MAX_ABS_EXPONENT = 128
_EXACT_CONTEXT_PRECISION = 2048


class BetfairSettlementRoundingError(ValueError):
    """Raised when a settlement-rounding input is outside the bounded contract."""


class ThresholdDisposition(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True, slots=True)
class BetfairOrdinarySettlementProjection:
    """Candidate structural ordinary non-BSP two-stage rounding projection.

    ``provider_applicability_proven`` and ``provider_posted_exact`` are hard
    false because a caller invoking this arithmetic helper is not proof that a
    specific provider market is governed by this exact rule pipeline.
    """

    gross_unrounded: Decimal
    commission_rate: Decimal
    gross_posted_model: Decimal
    commission_posted_model: Decimal
    net_posted_model: Decimal
    continuous_net: Decimal
    absolute_model_delta: Decimal
    model_error_bound_exclusive: Decimal = field(default=Decimal("0.01"), init=False)
    settlement_model: str = field(
        default="ORDINARY_NON_BSP_GROSS_HALF_UP_THEN_COMMISSION_HALF_UP_V1",
        init=False,
    )
    provider_applicability_proven: bool = field(default=False, init=False)
    provider_posted_exact: bool = field(default=False, init=False)
    execution_authorized: bool = field(default=False, init=False)
    real_money_execution: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        gross = _require_decimal("gross_unrounded", self.gross_unrounded)
        rate = _require_decimal("commission_rate", self.commission_rate, non_negative=True)
        if rate > 1:
            raise BetfairSettlementRoundingError("commission_rate must be <= 1")
        _require_decimal("gross_posted_model", self.gross_posted_model)
        _require_decimal("commission_posted_model", self.commission_posted_model, non_negative=True)
        _require_decimal("net_posted_model", self.net_posted_model)
        _require_decimal("continuous_net", self.continuous_net)
        _require_decimal("absolute_model_delta", self.absolute_model_delta, non_negative=True)

        gross_cents, expected_gross = _round_half_up_to_cents(gross)
        commission_base = expected_gross if expected_gross > 0 else Decimal("0")
        commission_cents, expected_commission = _round_half_up_to_cents(
            _exact_mul(rate, commission_base)
        )
        expected_net = _cents_to_decimal(gross_cents - commission_cents)
        expected_continuous = continuous_after_commission(gross, rate)
        expected_delta = _exact_sub(expected_net, expected_continuous)
        if expected_delta < 0:
            expected_delta = -expected_delta

        if self.gross_posted_model != expected_gross:
            raise BetfairSettlementRoundingError("gross_posted_model does not match model")
        if self.commission_posted_model != expected_commission:
            raise BetfairSettlementRoundingError("commission_posted_model does not match model")
        if self.net_posted_model != expected_net:
            raise BetfairSettlementRoundingError("net_posted_model does not match model")
        if self.continuous_net != expected_continuous:
            raise BetfairSettlementRoundingError("continuous_net does not match model")
        if self.absolute_model_delta != expected_delta:
            raise BetfairSettlementRoundingError("absolute_model_delta does not match model")
        if not expected_delta < self.model_error_bound_exclusive:
            raise BetfairSettlementRoundingError("two-stage rounding invariant violated")


@dataclass(frozen=True, slots=True)
class MonetaryUncertaintyInterval:
    center: Decimal
    epsilon: Decimal
    lower: Decimal
    upper: Decimal
    provider_posted_exact: bool = field(default=False, init=False)
    execution_authorized: bool = field(default=False, init=False)
    real_money_execution: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        center = _require_decimal("center", self.center)
        epsilon = _require_decimal("epsilon", self.epsilon, non_negative=True)
        lower = _require_decimal("lower", self.lower)
        upper = _require_decimal("upper", self.upper)
        if lower != _exact_sub(center, epsilon):
            raise BetfairSettlementRoundingError("lower does not match center - epsilon")
        if upper != _exact_add(center, epsilon):
            raise BetfairSettlementRoundingError("upper does not match center + epsilon")


def _require_decimal(name: str, value: Decimal, *, non_negative: bool = False) -> Decimal:
    if type(value) is not Decimal:
        raise BetfairSettlementRoundingError(f"{name} must be an exact Decimal")
    if not value.is_finite():
        raise BetfairSettlementRoundingError(f"{name} must be finite")
    parts = value.as_tuple()
    if len(parts.digits) > _MAX_COEFFICIENT_DIGITS:
        raise BetfairSettlementRoundingError(f"{name} coefficient is too large")
    if abs(parts.exponent) > _MAX_ABS_EXPONENT:
        raise BetfairSettlementRoundingError(f"{name} exponent is outside the bounded domain")
    if non_negative and value < 0:
        raise BetfairSettlementRoundingError(f"{name} must be non-negative")
    return value


def _exact_add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _EXACT_CONTEXT_PRECISION
        return left + right


def _exact_sub(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _EXACT_CONTEXT_PRECISION
        return left - right


def _exact_mul(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _EXACT_CONTEXT_PRECISION
        return left * right


def _cents_to_decimal(cents: int) -> Decimal:
    sign = "-" if cents < 0 else ""
    whole, fraction = divmod(abs(cents), 100)
    return Decimal(f"{sign}{whole}.{fraction:02d}")


def _round_half_up_to_cents(value: Decimal) -> tuple[int, Decimal]:
    numerator, denominator = value.as_integer_ratio()
    sign = -1 if numerator < 0 else 1
    numerator = abs(numerator)
    quotient, remainder = divmod(numerator * 100, denominator)
    if remainder * 2 >= denominator:
        quotient += 1
    cents = sign * quotient
    return cents, _cents_to_decimal(cents)


def continuous_after_commission(gross_unrounded: Decimal, commission_rate: Decimal) -> Decimal:
    """Return exact continuous-model net money without cent rounding."""

    gross = _require_decimal("gross_unrounded", gross_unrounded)
    rate = _require_decimal("commission_rate", commission_rate, non_negative=True)
    if rate > 1:
        raise BetfairSettlementRoundingError("commission_rate must be <= 1")
    commission_base = gross if gross > 0 else Decimal("0")
    return _exact_sub(gross, _exact_mul(rate, commission_base))


def project_ordinary_two_stage_rounding(
    gross_unrounded: Decimal,
    commission_rate: Decimal,
) -> BetfairOrdinarySettlementProjection:
    """Project one candidate ordinary two-stage cent-rounding shape.

    Public rule evidence does not prove that commission is calculated from the
    already rounded gross amount; this ordering is a bounded structural
    hypothesis for uncertainty analysis, not provider-applicability evidence.
    A positive consumer must separately establish that the exact market/product
    uses this ordinary non-BSP pipeline and that no more-specific rule overrides
    it.
    """

    gross = _require_decimal("gross_unrounded", gross_unrounded)
    rate = _require_decimal("commission_rate", commission_rate, non_negative=True)
    if rate > 1:
        raise BetfairSettlementRoundingError("commission_rate must be <= 1")

    gross_cents, gross_posted = _round_half_up_to_cents(gross)
    commission_base = gross_posted if gross_posted > 0 else Decimal("0")
    commission_unrounded = _exact_mul(rate, commission_base)
    commission_cents, commission_posted = _round_half_up_to_cents(commission_unrounded)
    net_posted = _cents_to_decimal(gross_cents - commission_cents)
    continuous_net = continuous_after_commission(gross, rate)
    delta = _exact_sub(net_posted, continuous_net)
    if delta < 0:
        delta = -delta

    # For this candidate structural ordering the gross-cent perturbation
    # contributes at most half a cent and the commission-charge rounding
    # contributes at most half a cent. The strict total bound is therefore
    # < 0.01 inside this model; it is not a provider-applicability proof.
    if not delta < Decimal("0.01"):
        raise BetfairSettlementRoundingError("two-stage rounding invariant violated")

    return BetfairOrdinarySettlementProjection(
        gross_unrounded=gross,
        commission_rate=rate,
        gross_posted_model=gross_posted,
        commission_posted_model=commission_posted,
        net_posted_model=net_posted,
        continuous_net=continuous_net,
        absolute_model_delta=delta,
    )


def monetary_uncertainty_interval(center: Decimal, epsilon: Decimal) -> MonetaryUncertaintyInterval:
    """Build a conservative closed interval from an externally justified epsilon.

    This function does not decide whether ``epsilon`` is provider-authoritative;
    it only preserves the supplied bounded uncertainty mechanically.
    """

    value = _require_decimal("center", center)
    bound = _require_decimal("epsilon", epsilon, non_negative=True)
    return MonetaryUncertaintyInterval(
        center=value,
        epsilon=bound,
        lower=_exact_sub(value, bound),
        upper=_exact_add(value, bound),
    )


def classify_strictly_above_threshold(
    interval: MonetaryUncertaintyInterval,
    threshold: Decimal,
) -> ThresholdDisposition:
    """Classify ``money > threshold`` only when the whole interval decides it."""

    if type(interval) is not MonetaryUncertaintyInterval:
        raise BetfairSettlementRoundingError(
            "interval must be canonical MonetaryUncertaintyInterval"
        )
    frozen_threshold = _require_decimal("threshold", threshold)
    if interval.lower > frozen_threshold:
        return ThresholdDisposition.PASS
    if interval.upper <= frozen_threshold:
        return ThresholdDisposition.FAIL
    return ThresholdDisposition.INDETERMINATE
