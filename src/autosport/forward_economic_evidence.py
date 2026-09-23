from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from typing import Protocol


_DECIMAL_PRECISION = 80
_MAX_EXACT_MONEY_COEFFICIENT_DIGITS = 512
_MAX_CANONICAL_DECIMAL_TEXT_CHARS = 512


class ForwardEconomicEvidenceError(ValueError):
    """Fail-closed validation error for prospective economic evidence."""


class BetSide(StrEnum):
    BACK = "BACK"
    LAY = "LAY"
    NONE = "NONE"


class EconomicCostCausality(StrEnum):
    DECISION_CAUSED = "DECISION_CAUSED"
    PREEXISTING_SHARED = "PREEXISTING_SHARED"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ForwardEconomicEvidenceError(f"{name} must be a non-empty canonical string")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ForwardEconomicEvidenceError(f"{name} must be a SHA-256 hex digest")
    return text


def _currency_code(value: object, name: str) -> str:
    text = _text(value, name)
    if (
        len(text) != 3
        or not text.isascii()
        or not text.isalpha()
        or text != text.upper()
    ):
        raise ForwardEconomicEvidenceError(
            f"{name} must be a canonical uppercase three-letter currency code"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ForwardEconomicEvidenceError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ForwardEconomicEvidenceError(f"{name} must be a finite Decimal")
    return value


def _positive_decimal(value: object, name: str) -> Decimal:
    result = _decimal(value, name)
    if result <= 0:
        raise ForwardEconomicEvidenceError(f"{name} must be positive")
    return result


def _exact_decimal_sum(*values: Decimal) -> Decimal:
    """Add finite Decimals exactly with bounded coefficient materialization."""

    if not values:
        return Decimal(0)

    parts: list[tuple[int, tuple[int, ...], int]] = []
    common_exponent: int | None = None
    for index, value in enumerate(values):
        decimal_value = _decimal(value, f"exact_decimal_sum[{index}]")
        decimal_tuple = decimal_value.as_tuple()
        exponent = decimal_tuple.exponent
        if type(exponent) is not int:
            raise ForwardEconomicEvidenceError(
                "exact decimal sum requires finite integral exponents"
            )
        digits = decimal_tuple.digits
        if len(digits) > _MAX_EXACT_MONEY_COEFFICIENT_DIGITS:
            raise ForwardEconomicEvidenceError(
                "exact money coefficient exceeds resource bound"
            )
        if all(digit == 0 for digit in digits):
            continue
        sign = -1 if decimal_tuple.sign else 1
        parts.append((sign, digits, exponent))
        common_exponent = (
            exponent
            if common_exponent is None
            else min(common_exponent, exponent)
        )

    if common_exponent is None:
        return Decimal(0)

    aligned_parts: list[tuple[int, tuple[int, ...], int]] = []
    for sign, digits, exponent in parts:
        shift = exponent - common_exponent
        if len(digits) + shift > _MAX_EXACT_MONEY_COEFFICIENT_DIGITS:
            raise ForwardEconomicEvidenceError(
                "exact money exponent gap exceeds resource bound"
            )
        aligned_parts.append((sign, digits, shift))

    total_coefficient = 0
    for sign, digits, shift in aligned_parts:
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        total_coefficient += sign * coefficient * (10 ** shift)

    if total_coefficient == 0:
        return Decimal(0)

    total_tuple = Decimal(total_coefficient).as_tuple()
    if len(total_tuple.digits) > _MAX_EXACT_MONEY_COEFFICIENT_DIGITS:
        raise ForwardEconomicEvidenceError(
            "exact money sum exceeds resource bound"
        )
    return Decimal((total_tuple.sign, total_tuple.digits, common_exponent))


def _decimal_text(value: Decimal) -> str:
    value = _decimal(value, "decimal")
    decimal_tuple = value.as_tuple()
    exponent = decimal_tuple.exponent
    if type(exponent) is not int:
        raise ForwardEconomicEvidenceError(
            "canonical decimal text requires a finite integral exponent"
        )
    if value.is_zero():
        return "0"

    # Match the existing fixed-point canonicalization without first expanding a
    # compact Decimal into an attacker-sized string.  Only fractional trailing
    # zeroes are discarded; integer zeroes remain part of the exact value text.
    digits = decimal_tuple.digits
    while exponent < 0 and len(digits) > 1 and digits[-1] == 0:
        digits = digits[:-1]
        exponent += 1

    point = len(digits) + exponent
    sign_chars = 1 if decimal_tuple.sign else 0
    if exponent >= 0:
        fixed_text_chars = sign_chars + len(digits) + exponent
    elif point > 0:
        fixed_text_chars = sign_chars + len(digits) + 1
    else:
        fixed_text_chars = sign_chars + 2 - point + len(digits)
    if fixed_text_chars > _MAX_CANONICAL_DECIMAL_TEXT_CHARS:
        raise ForwardEconomicEvidenceError(
            "canonical decimal text exceeds resource bound"
        )

    canonical_value = Decimal((decimal_tuple.sign, digits, exponent))
    return format(canonical_value, "f")


def _instant_text(value: datetime) -> str:
    return _instant(value, "instant").isoformat().replace("+00:00", "Z")


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _log_threshold(alpha: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _DECIMAL_PRECISION
        return -(+alpha).ln()


def _log_e_increment(lam: Decimal, value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    if low > value or value > high:
        raise ForwardEconomicEvidenceError("normalized payoff lies outside its pre-outcome bound")
    with localcontext() as context:
        context.prec = _DECIMAL_PRECISION
        width = high - low
        return +(lam * value - (lam * lam * width * width) / Decimal(8))


@dataclass(frozen=True, slots=True)
class AlphaAllocation:
    challenger_id: str
    alpha: Decimal

    def __post_init__(self) -> None:
        _text(self.challenger_id, "challenger_id")
        alpha = _positive_decimal(self.alpha, "alpha")
        # Alpha reaches exact Fraction arithmetic below. Reuse the canonical
        # Decimal representation bound before any rational materialization.
        _decimal_text(alpha)
        if alpha >= 1:
            raise ForwardEconomicEvidenceError("alpha must be less than one")

    def to_payload(self) -> dict[str, object]:
        return {
            "challenger_id": self.challenger_id,
            "alpha": _decimal_text(self.alpha),
        }


@dataclass(frozen=True, slots=True)
class FamilywiseAlphaRegistry:
    family_id: str
    total_alpha: Decimal
    allocations: tuple[AlphaAllocation, ...]
    sealed_at: datetime
    _sealed_identity_sha256: str = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        _text(self.family_id, "family_id")
        total = _positive_decimal(self.total_alpha, "total_alpha")
        # total_alpha is also converted to Fraction for the family budget.
        # Reject attacker-sized compact Decimal representations first.
        _decimal_text(total)
        if total >= 1:
            raise ForwardEconomicEvidenceError("total_alpha must be less than one")
        sealed_at = _instant(self.sealed_at, "sealed_at")

        # Snapshot caller-owned container/items exactly once before validating
        # them. Validation must not iterate a caller-mutable collection and then
        # trust a later, potentially different iteration as the sealed family.
        raw_allocations = tuple(self.allocations)
        if not raw_allocations:
            raise ForwardEconomicEvidenceError("alpha registry must allocate at least one challenger")
        if any(type(item) is not AlphaAllocation for item in raw_allocations):
            raise ForwardEconomicEvidenceError("alpha allocations must be exact AlphaAllocation values")

        # An exact frozen dataclass can still be low-level mutated through
        # object.__setattr__ before it reaches this sealing boundary. Snapshot
        # only exact values, then reconstruct them through the canonical
        # AlphaAllocation validator so pre-seal drift cannot bypass positivity
        # or Decimal resource bounds before Fraction budget arithmetic.
        allocations = tuple(
            AlphaAllocation(
                challenger_id=snapshot.challenger_id,
                alpha=snapshot.alpha,
            )
            for snapshot in (deepcopy(item) for item in raw_allocations)
        )
        challenger_ids = tuple(item.challenger_id for item in allocations)
        if challenger_ids != tuple(sorted(challenger_ids)):
            raise ForwardEconomicEvidenceError("alpha allocations must be sorted by challenger_id")
        if len(set(challenger_ids)) != len(challenger_ids):
            raise ForwardEconomicEvidenceError("alpha allocations must have unique challenger_id values")
        allocated = sum((Fraction(item.alpha) for item in allocations), Fraction(0))
        if allocated > Fraction(total):
            raise ForwardEconomicEvidenceError("familywise alpha allocations exceed total_alpha")

        object.__setattr__(self, "total_alpha", total)
        object.__setattr__(self, "allocations", allocations)
        object.__setattr__(self, "sealed_at", sealed_at)
        object.__setattr__(
            self,
            "_sealed_identity_sha256",
            _canonical_digest(self._payload_unchecked()),
        )

    def _payload_unchecked(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "family_id": self.family_id,
            "total_alpha": _decimal_text(self.total_alpha),
            "sealed_at": _instant_text(self.sealed_at),
            "allocations": [item.to_payload() for item in self.allocations],
        }

    def _require_sealed_identity(self) -> None:
        if _canonical_digest(self._payload_unchecked()) != self._sealed_identity_sha256:
            raise ForwardEconomicEvidenceError(
                "sealed alpha registry identity integrity drift"
            )

    def allocation_for(self, challenger_id: str) -> Decimal:
        self._require_sealed_identity()
        challenger_id = _text(challenger_id, "challenger_id")
        for item in self.allocations:
            if item.challenger_id == challenger_id:
                return item.alpha
        raise ForwardEconomicEvidenceError("challenger is not preallocated in alpha registry")

    def to_payload(self) -> dict[str, object]:
        self._require_sealed_identity()
        return self._payload_unchecked()

    @property
    def identity_sha256(self) -> str:
        self._require_sealed_identity()
        return self._sealed_identity_sha256


@dataclass(frozen=True, slots=True, init=False)
class ForwardEconomicProtocol:
    protocol_id: str
    challenger_id: str
    champion_id: str
    universe_id: str
    universe_sha256: str
    authority_binding_sha256: str
    currency_code: str | None
    denomination_authority_sha256: str | None
    alpha_registry_sha256: str
    challenger_alpha: Decimal
    minimum_events: int
    risk_unit_currency: Decimal
    maximum_accepted_odds: Decimal
    maximum_drawdown_currency: Decimal
    maximum_economic_cost_currency: Decimal
    absolute_lambda: Decimal
    paired_lambda: Decimal
    start_sequence: int
    frozen_at: datetime

    def __init__(
        self,
        *,
        protocol_id: str,
        challenger_id: str,
        champion_id: str,
        universe_id: str,
        universe_sha256: str,
        authority_binding_sha256: str,
        alpha_registry: FamilywiseAlphaRegistry,
        minimum_events: int,
        risk_unit_currency: Decimal,
        maximum_accepted_odds: Decimal,
        maximum_drawdown_currency: Decimal,
        absolute_lambda: Decimal,
        paired_lambda: Decimal,
        start_sequence: int,
        frozen_at: datetime,
        currency_code: str | None = None,
        denomination_authority_sha256: str | None = None,
        maximum_economic_cost_currency: Decimal = Decimal(0),
    ) -> None:
        if type(alpha_registry) is not FamilywiseAlphaRegistry:
            raise ForwardEconomicEvidenceError("alpha_registry must be an exact FamilywiseAlphaRegistry")
        protocol_id = _text(protocol_id, "protocol_id")
        challenger_id = _text(challenger_id, "challenger_id")
        champion_id = _text(champion_id, "champion_id")
        universe_id = _text(universe_id, "universe_id")
        universe_sha256 = _sha256(universe_sha256, "universe_sha256")
        authority_binding_sha256 = _sha256(
            authority_binding_sha256, "authority_binding_sha256"
        )
        if (currency_code is None) != (denomination_authority_sha256 is None):
            raise ForwardEconomicEvidenceError(
                "currency_code and denomination_authority_sha256 must be bound together"
            )
        if currency_code is not None:
            currency_code = _currency_code(currency_code, "currency_code")
            denomination_authority_sha256 = _sha256(
                denomination_authority_sha256,
                "denomination_authority_sha256",
            )
        if challenger_id == champion_id:
            raise ForwardEconomicEvidenceError("challenger and champion must be distinct")
        alpha = alpha_registry.allocation_for(challenger_id)
        if isinstance(minimum_events, bool) or not isinstance(minimum_events, int) or minimum_events <= 0:
            raise ForwardEconomicEvidenceError("minimum_events must be a positive integer")
        risk_unit = _positive_decimal(risk_unit_currency, "risk_unit_currency")
        maximum_odds = _positive_decimal(maximum_accepted_odds, "maximum_accepted_odds")
        if maximum_odds <= 1:
            raise ForwardEconomicEvidenceError("maximum_accepted_odds must exceed one")
        drawdown = _decimal(maximum_drawdown_currency, "maximum_drawdown_currency")
        if drawdown < 0:
            raise ForwardEconomicEvidenceError("maximum_drawdown_currency must be non-negative")
        maximum_cost = _decimal(
            maximum_economic_cost_currency,
            "maximum_economic_cost_currency",
        )
        if maximum_cost < 0:
            raise ForwardEconomicEvidenceError(
                "maximum_economic_cost_currency must be non-negative"
            )
        abs_lambda = _positive_decimal(absolute_lambda, "absolute_lambda")
        pair_lambda = _positive_decimal(paired_lambda, "paired_lambda")
        if isinstance(start_sequence, bool) or not isinstance(start_sequence, int) or start_sequence < 0:
            raise ForwardEconomicEvidenceError("start_sequence must be a non-negative integer")
        frozen = _instant(frozen_at, "frozen_at")
        if alpha_registry.sealed_at > frozen:
            raise ForwardEconomicEvidenceError("alpha registry must be sealed before protocol freeze")
        for name, value in (
            ("protocol_id", protocol_id),
            ("challenger_id", challenger_id),
            ("champion_id", champion_id),
            ("universe_id", universe_id),
            ("universe_sha256", universe_sha256),
            ("authority_binding_sha256", authority_binding_sha256),
            ("currency_code", currency_code),
            ("denomination_authority_sha256", denomination_authority_sha256),
            ("alpha_registry_sha256", alpha_registry.identity_sha256),
            ("challenger_alpha", alpha),
            ("minimum_events", minimum_events),
            ("risk_unit_currency", risk_unit),
            ("maximum_accepted_odds", maximum_odds),
            ("maximum_drawdown_currency", drawdown),
            ("maximum_economic_cost_currency", maximum_cost),
            ("absolute_lambda", abs_lambda),
            ("paired_lambda", pair_lambda),
            ("start_sequence", start_sequence),
            ("frozen_at", frozen),
        ):
            object.__setattr__(self, name, value)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 4,
            "protocol_id": self.protocol_id,
            "challenger_id": self.challenger_id,
            "champion_id": self.champion_id,
            "universe_id": self.universe_id,
            "universe_sha256": self.universe_sha256,
            "authority_binding_sha256": self.authority_binding_sha256,
            "currency_code": self.currency_code,
            "denomination_authority_sha256": self.denomination_authority_sha256,
            "alpha_registry_sha256": self.alpha_registry_sha256,
            "challenger_alpha": _decimal_text(self.challenger_alpha),
            "minimum_events": self.minimum_events,
            "risk_unit_currency": _decimal_text(self.risk_unit_currency),
            "maximum_accepted_odds": _decimal_text(self.maximum_accepted_odds),
            "maximum_drawdown_currency": _decimal_text(self.maximum_drawdown_currency),
            "maximum_economic_cost_currency": _decimal_text(
                self.maximum_economic_cost_currency
            ),
            "absolute_lambda": _decimal_text(self.absolute_lambda),
            "paired_lambda": _decimal_text(self.paired_lambda),
            "start_sequence": self.start_sequence,
            "frozen_at": _instant_text(self.frozen_at),
            "decimal_precision": _DECIMAL_PRECISION,
            "normalization": "all_in_net_money_pnl_divided_by_fixed_risk_unit",
            "economic_cost_support": "frozen_non_negative_cost_bound",
            "absolute_null": "mean_normalized_challenger_money_pnl<=0",
            "paired_null": "mean_normalized_challenger_minus_champion_money_pnl<=0",
        }

    @property
    def identity_sha256(self) -> str:
        return _canonical_digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class ForwardDecisionObservation:
    sequence: int
    universe_sha256: str
    universe_event_sha256: str
    challenger_decision_sha256: str
    champion_decision_sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ForwardEconomicEvidenceError("sequence must be a non-negative integer")
        object.__setattr__(
            self,
            "universe_sha256",
            _sha256(self.universe_sha256, "universe_sha256"),
        )
        object.__setattr__(
            self,
            "universe_event_sha256",
            _sha256(self.universe_event_sha256, "universe_event_sha256"),
        )
        object.__setattr__(
            self,
            "challenger_decision_sha256",
            _sha256(self.challenger_decision_sha256, "challenger_decision_sha256"),
        )
        object.__setattr__(
            self,
            "champion_decision_sha256",
            _sha256(self.champion_decision_sha256, "champion_decision_sha256"),
        )


@dataclass(frozen=True, slots=True)
class ResolvedPolicyOutcome:
    policy_id: str
    sequence: int
    universe_event_sha256: str
    decision_sha256: str
    decision_committed_at: datetime
    side: BetSide
    accepted_odds: Decimal | None
    accepted_stake: Decimal | None
    net_pnl_currency: Decimal
    execution_evidence_sha256: str | None
    execution_accepted_at: datetime | None
    settlement_evidence_sha256: str | None
    settlement_available_at: datetime | None
    currency_code: str | None = None
    denomination_authority_sha256: str | None = None
    wager_pnl_currency: Decimal | None = None
    economic_cost_currency: Decimal = Decimal(0)
    economic_cost_evidence_sha256: str | None = None
    economic_cost_incurred_at: datetime | None = None
    economic_cost_available_at: datetime | None = None
    economic_cost_causality: EconomicCostCausality = EconomicCostCausality.DECISION_CAUSED
    economic_cost_class_id: str | None = None
    economic_cost_allocation_authority_sha256: str | None = None

    def __post_init__(self) -> None:
        _text(self.policy_id, "policy_id")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ForwardEconomicEvidenceError("sequence must be a non-negative integer")
        object.__setattr__(
            self,
            "universe_event_sha256",
            _sha256(self.universe_event_sha256, "universe_event_sha256"),
        )
        object.__setattr__(
            self,
            "decision_sha256",
            _sha256(self.decision_sha256, "decision_sha256"),
        )
        committed = _instant(self.decision_committed_at, "decision_committed_at")
        object.__setattr__(self, "decision_committed_at", committed)
        if type(self.side) is not BetSide:
            raise ForwardEconomicEvidenceError("side must be an exact BetSide")
        pnl = _decimal(self.net_pnl_currency, "net_pnl_currency")
        cost = _decimal(self.economic_cost_currency, "economic_cost_currency")
        # These externally supplied money values may reach exact Fraction
        # arithmetic below. Bound their canonical representation before any
        # rational denominator/coefficient materialization.
        _decimal_text(pnl)
        _decimal_text(cost)
        if cost < 0:
            raise ForwardEconomicEvidenceError(
                "economic_cost_currency must be non-negative"
            )
        if type(self.economic_cost_causality) is not EconomicCostCausality:
            raise ForwardEconomicEvidenceError(
                "economic_cost_causality must be an exact EconomicCostCausality"
            )
        if self.economic_cost_causality is EconomicCostCausality.PREEXISTING_SHARED:
            if cost <= 0:
                raise ForwardEconomicEvidenceError(
                    "PREEXISTING_SHARED requires a positive economic cost"
                )
            if (
                self.economic_cost_class_id is None
                or self.economic_cost_allocation_authority_sha256 is None
            ):
                raise ForwardEconomicEvidenceError(
                    "PREEXISTING_SHARED requires canonical cost class and allocation authority"
                )
            object.__setattr__(
                self,
                "economic_cost_class_id",
                _text(self.economic_cost_class_id, "economic_cost_class_id"),
            )
            object.__setattr__(
                self,
                "economic_cost_allocation_authority_sha256",
                _sha256(
                    self.economic_cost_allocation_authority_sha256,
                    "economic_cost_allocation_authority_sha256",
                ),
            )
        elif (
            self.economic_cost_class_id is not None
            or self.economic_cost_allocation_authority_sha256 is not None
        ):
            raise ForwardEconomicEvidenceError(
                "shared cost allocation metadata requires PREEXISTING_SHARED causality"
            )
        if (self.economic_cost_evidence_sha256 is None) != (
            self.economic_cost_available_at is None
        ):
            raise ForwardEconomicEvidenceError(
                "economic cost evidence and availability must be bound together"
            )
        cost_incurred = None
        if self.economic_cost_incurred_at is not None:
            if self.economic_cost_evidence_sha256 is None:
                raise ForwardEconomicEvidenceError(
                    "economic cost incidence requires canonical cost evidence"
                )
            cost_incurred = _instant(
                self.economic_cost_incurred_at,
                "economic_cost_incurred_at",
            )
            object.__setattr__(
                self,
                "economic_cost_incurred_at",
                cost_incurred,
            )
            if (
                cost_incurred < committed
                and self.economic_cost_causality
                is not EconomicCostCausality.PREEXISTING_SHARED
            ):
                raise ForwardEconomicEvidenceError(
                    "economic cost cannot be incurred before the committed decision"
                )
        cost_available = None
        if self.economic_cost_evidence_sha256 is not None:
            object.__setattr__(
                self,
                "economic_cost_evidence_sha256",
                _sha256(
                    self.economic_cost_evidence_sha256,
                    "economic_cost_evidence_sha256",
                ),
            )
            cost_available = _instant(
                self.economic_cost_available_at,
                "economic_cost_available_at",
            )
            if (
                cost_available < committed
                and self.economic_cost_causality
                is not EconomicCostCausality.PREEXISTING_SHARED
            ):
                raise ForwardEconomicEvidenceError(
                    "economic cost cannot become available before the committed decision"
                )
            if cost_incurred is not None and cost_available < cost_incurred:
                raise ForwardEconomicEvidenceError(
                    "economic cost evidence cannot become available before cost incidence"
                )
        elif cost != 0:
            raise ForwardEconomicEvidenceError(
                "non-zero economic cost requires canonical evidence and availability"
            )
        if cost != 0 and cost_incurred is None:
            raise ForwardEconomicEvidenceError(
                "non-zero economic cost requires canonical incurred/effective time"
            )
        if self.wager_pnl_currency is None:
            if cost == 0:
                wager_pnl = pnl
            elif self.side is BetSide.NONE:
                if Fraction(pnl) + Fraction(cost) != 0:
                    raise ForwardEconomicEvidenceError(
                        "NONE wager P&L must be exactly zero"
                    )
                wager_pnl = Decimal(0)
            else:
                raise ForwardEconomicEvidenceError(
                    "executed all-in cost requires explicit wager P&L"
                )
        else:
            wager_pnl = _decimal(
                self.wager_pnl_currency,
                "wager_pnl_currency",
            )
            _decimal_text(wager_pnl)
            if Fraction(pnl) != Fraction(wager_pnl) - Fraction(cost):
                raise ForwardEconomicEvidenceError(
                    "all-in net P&L must equal wager P&L minus economic cost"
                )
        if (self.currency_code is None) != (
            self.denomination_authority_sha256 is None
        ):
            raise ForwardEconomicEvidenceError(
                "resolved currency_code and denomination authority must be bound together"
            )
        if self.currency_code is not None:
            _currency_code(self.currency_code, "currency_code")
            object.__setattr__(
                self,
                "denomination_authority_sha256",
                _sha256(
                    self.denomination_authority_sha256,
                    "denomination_authority_sha256",
                ),
            )
        if self.side is BetSide.NONE:
            if self.accepted_odds is not None or self.accepted_stake is not None:
                raise ForwardEconomicEvidenceError("NONE cannot carry accepted odds or stake")
            if (
                self.execution_evidence_sha256 is not None
                or self.settlement_evidence_sha256 is not None
            ):
                raise ForwardEconomicEvidenceError(
                    "NONE cannot carry execution or settlement evidence"
                )
            if (
                self.execution_accepted_at is not None
                or self.settlement_available_at is not None
            ):
                raise ForwardEconomicEvidenceError(
                    "NONE cannot carry execution acceptance or settlement availability"
                )
            if wager_pnl != 0:
                if cost == 0:
                    raise ForwardEconomicEvidenceError(
                        "NONE must have exactly zero money P&L"
                    )
                raise ForwardEconomicEvidenceError(
                    "NONE wager P&L must be exactly zero"
                )
            return
        odds = _positive_decimal(self.accepted_odds, "accepted_odds")
        if odds <= 1:
            raise ForwardEconomicEvidenceError("accepted_odds must exceed one")
        stake = _positive_decimal(self.accepted_stake, "accepted_stake")
        # Odds/stake feed exact wager-domain and risk arithmetic below. Bound
        # their canonical Decimal representations before Fraction materialization.
        _decimal_text(odds)
        _decimal_text(stake)
        object.__setattr__(
            self,
            "execution_evidence_sha256",
            _sha256(self.execution_evidence_sha256, "execution_evidence_sha256"),
        )
        accepted = _instant(self.execution_accepted_at, "execution_accepted_at")
        object.__setattr__(
            self,
            "settlement_evidence_sha256",
            _sha256(self.settlement_evidence_sha256, "settlement_evidence_sha256"),
        )
        available = _instant(self.settlement_available_at, "settlement_available_at")
        if accepted <= committed:
            raise ForwardEconomicEvidenceError(
                "execution acceptance must be strictly after the committed decision"
            )
        if available <= accepted:
            raise ForwardEconomicEvidenceError(
                "settlement must become available after accepted execution"
            )
        odds_fraction = Fraction(odds)
        stake_fraction = Fraction(stake)
        wager_pnl_fraction = Fraction(wager_pnl)
        if self.side is BetSide.BACK:
            low_fraction = -stake_fraction
            high_fraction = (odds_fraction - 1) * stake_fraction
        else:
            low_fraction = -((odds_fraction - 1) * stake_fraction)
            high_fraction = stake_fraction
        if wager_pnl_fraction < low_fraction or wager_pnl_fraction > high_fraction:
            raise ForwardEconomicEvidenceError(
                "wager P&L is outside accepted side/odds/stake bounds"
            )

    @property
    def effective_wager_pnl_currency(self) -> Decimal:
        if self.wager_pnl_currency is None:
            return _exact_decimal_sum(
                self.net_pnl_currency,
                self.economic_cost_currency,
            )
        return self.wager_pnl_currency

    @property
    def economic_available_at(self) -> datetime | None:
        instants = tuple(
            instant
            for instant in (
                self.settlement_available_at,
                self.economic_cost_available_at,
            )
            if instant is not None
        )
        return max(instants) if instants else None

    @property
    def economic_cost_complete(self) -> bool:
        return (
            self.economic_cost_evidence_sha256 is not None
            and (
                self.economic_cost_currency == 0
                or self.economic_cost_incurred_at is not None
            )
        )


class EconomicAuthorityResolver(Protocol):
    """Integration seam: product code resolves canonical execution+settlement truth."""

    @property
    def authority_sha256(self) -> str: ...

    def resolve(
        self,
        *,
        policy_id: str,
        sequence: int,
        universe_event_sha256: str,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome: ...


@dataclass(frozen=True, slots=True)
class ForwardEconomicStep:
    sequence: int
    universe_event_sha256: str
    challenger_decision_sha256: str
    champion_decision_sha256: str
    challenger_decision_committed_at: datetime
    champion_decision_committed_at: datetime
    challenger_side: BetSide
    champion_side: BetSide
    challenger_net_pnl_currency: Decimal
    champion_net_pnl_currency: Decimal
    challenger_wager_pnl_currency: Decimal
    champion_wager_pnl_currency: Decimal
    challenger_economic_cost_currency: Decimal
    champion_economic_cost_currency: Decimal
    challenger_economic_cost_evidence_sha256: str | None
    champion_economic_cost_evidence_sha256: str | None
    challenger_economic_cost_incurred_at: datetime | None
    champion_economic_cost_incurred_at: datetime | None
    challenger_economic_cost_complete: bool
    champion_economic_cost_complete: bool
    challenger_economic_cost_available_at: datetime | None
    champion_economic_cost_available_at: datetime | None
    challenger_economic_available_at: datetime | None
    champion_economic_available_at: datetime | None
    currency_code: str | None
    denomination_authority_sha256: str | None
    challenger_normalized_pnl: Decimal
    paired_normalized_pnl: Decimal
    absolute_low: Decimal
    absolute_high: Decimal
    paired_low: Decimal
    paired_high: Decimal
    absolute_log_e_after: Decimal
    paired_log_e_after: Decimal
    challenger_execution_evidence_sha256: str | None
    challenger_execution_accepted_at: datetime | None
    challenger_settlement_evidence_sha256: str | None
    challenger_settlement_available_at: datetime | None
    champion_execution_evidence_sha256: str | None
    champion_execution_accepted_at: datetime | None
    champion_settlement_evidence_sha256: str | None
    champion_settlement_available_at: datetime | None
    challenger_economic_cost_causality: EconomicCostCausality = (
        EconomicCostCausality.DECISION_CAUSED
    )
    champion_economic_cost_causality: EconomicCostCausality = (
        EconomicCostCausality.DECISION_CAUSED
    )
    challenger_economic_cost_class_id: str | None = None
    champion_economic_cost_class_id: str | None = None
    challenger_economic_cost_allocation_authority_sha256: str | None = None
    champion_economic_cost_allocation_authority_sha256: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "universe_event_sha256": self.universe_event_sha256,
            "challenger_decision_sha256": self.challenger_decision_sha256,
            "champion_decision_sha256": self.champion_decision_sha256,
            "challenger_decision_committed_at": _instant_text(
                self.challenger_decision_committed_at
            ),
            "champion_decision_committed_at": _instant_text(
                self.champion_decision_committed_at
            ),
            "challenger_side": self.challenger_side.value,
            "champion_side": self.champion_side.value,
            "challenger_net_pnl_currency": _decimal_text(self.challenger_net_pnl_currency),
            "champion_net_pnl_currency": _decimal_text(self.champion_net_pnl_currency),
            "challenger_wager_pnl_currency": _decimal_text(
                self.challenger_wager_pnl_currency
            ),
            "champion_wager_pnl_currency": _decimal_text(
                self.champion_wager_pnl_currency
            ),
            "challenger_economic_cost_currency": _decimal_text(
                self.challenger_economic_cost_currency
            ),
            "champion_economic_cost_currency": _decimal_text(
                self.champion_economic_cost_currency
            ),
            "challenger_economic_cost_evidence_sha256": (
                self.challenger_economic_cost_evidence_sha256
            ),
            "champion_economic_cost_evidence_sha256": (
                self.champion_economic_cost_evidence_sha256
            ),
            "challenger_economic_cost_causality": (
                self.challenger_economic_cost_causality.value
            ),
            "champion_economic_cost_causality": (
                self.champion_economic_cost_causality.value
            ),
            "challenger_economic_cost_class_id": (
                self.challenger_economic_cost_class_id
            ),
            "champion_economic_cost_class_id": self.champion_economic_cost_class_id,
            "challenger_economic_cost_allocation_authority_sha256": (
                self.challenger_economic_cost_allocation_authority_sha256
            ),
            "champion_economic_cost_allocation_authority_sha256": (
                self.champion_economic_cost_allocation_authority_sha256
            ),
            "challenger_economic_cost_incurred_at": (
                None
                if self.challenger_economic_cost_incurred_at is None
                else _instant_text(self.challenger_economic_cost_incurred_at)
            ),
            "champion_economic_cost_incurred_at": (
                None
                if self.champion_economic_cost_incurred_at is None
                else _instant_text(self.champion_economic_cost_incurred_at)
            ),
            "challenger_economic_cost_complete": (
                self.challenger_economic_cost_complete
            ),
            "champion_economic_cost_complete": self.champion_economic_cost_complete,
            "challenger_economic_cost_available_at": (
                None
                if self.challenger_economic_cost_available_at is None
                else _instant_text(self.challenger_economic_cost_available_at)
            ),
            "champion_economic_cost_available_at": (
                None
                if self.champion_economic_cost_available_at is None
                else _instant_text(self.champion_economic_cost_available_at)
            ),
            "challenger_economic_available_at": (
                None
                if self.challenger_economic_available_at is None
                else _instant_text(self.challenger_economic_available_at)
            ),
            "champion_economic_available_at": (
                None
                if self.champion_economic_available_at is None
                else _instant_text(self.champion_economic_available_at)
            ),
            "currency_code": self.currency_code,
            "denomination_authority_sha256": self.denomination_authority_sha256,
            "challenger_normalized_pnl": _decimal_text(self.challenger_normalized_pnl),
            "paired_normalized_pnl": _decimal_text(self.paired_normalized_pnl),
            "absolute_low": _decimal_text(self.absolute_low),
            "absolute_high": _decimal_text(self.absolute_high),
            "paired_low": _decimal_text(self.paired_low),
            "paired_high": _decimal_text(self.paired_high),
            "absolute_log_e_after": _decimal_text(self.absolute_log_e_after),
            "paired_log_e_after": _decimal_text(self.paired_log_e_after),
            "challenger_execution_evidence_sha256": self.challenger_execution_evidence_sha256,
            "challenger_execution_accepted_at": (
                None
                if self.challenger_execution_accepted_at is None
                else _instant_text(self.challenger_execution_accepted_at)
            ),
            "challenger_settlement_evidence_sha256": self.challenger_settlement_evidence_sha256,
            "challenger_settlement_available_at": (
                None
                if self.challenger_settlement_available_at is None
                else _instant_text(self.challenger_settlement_available_at)
            ),
            "champion_execution_evidence_sha256": self.champion_execution_evidence_sha256,
            "champion_execution_accepted_at": (
                None
                if self.champion_execution_accepted_at is None
                else _instant_text(self.champion_execution_accepted_at)
            ),
            "champion_settlement_evidence_sha256": self.champion_settlement_evidence_sha256,
            "champion_settlement_available_at": (
                None
                if self.champion_settlement_available_at is None
                else _instant_text(self.champion_settlement_available_at)
            ),
        }


@dataclass(frozen=True, slots=True)
class ForwardEconomicEvidenceSummary:
    protocol_sha256: str
    currency_code: str | None
    denomination_authority_sha256: str | None
    denomination_bound: bool
    all_in_economics_complete: bool
    observed_events: int
    next_sequence: int
    challenger_total_pnl_currency: Decimal
    champion_total_pnl_currency: Decimal
    challenger_peak_pnl_currency: Decimal
    challenger_max_drawdown_currency: Decimal
    absolute_log_e: Decimal
    paired_log_e: Decimal
    log_threshold: Decimal
    absolute_threshold_crossed: bool
    paired_threshold_crossed: bool
    minimum_events_satisfied: bool
    drawdown_guard_passed: bool
    positive_authority_verified: bool
    conditional_eprocess_verified: bool
    scientific_promotion_gate_passed: bool
    evidence_sha256: str
    promotion_authority: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "protocol_sha256": self.protocol_sha256,
            "currency_code": self.currency_code,
            "denomination_authority_sha256": self.denomination_authority_sha256,
            "denomination_bound": self.denomination_bound,
            "all_in_economics_complete": self.all_in_economics_complete,
            "observed_events": self.observed_events,
            "next_sequence": self.next_sequence,
            "challenger_total_pnl_currency": _decimal_text(self.challenger_total_pnl_currency),
            "champion_total_pnl_currency": _decimal_text(self.champion_total_pnl_currency),
            "challenger_peak_pnl_currency": _decimal_text(self.challenger_peak_pnl_currency),
            "challenger_max_drawdown_currency": _decimal_text(self.challenger_max_drawdown_currency),
            "absolute_log_e": _decimal_text(self.absolute_log_e),
            "paired_log_e": _decimal_text(self.paired_log_e),
            "log_threshold": _decimal_text(self.log_threshold),
            "absolute_threshold_crossed": self.absolute_threshold_crossed,
            "paired_threshold_crossed": self.paired_threshold_crossed,
            "minimum_events_satisfied": self.minimum_events_satisfied,
            "drawdown_guard_passed": self.drawdown_guard_passed,
            "positive_authority_verified": self.positive_authority_verified,
            "conditional_eprocess_verified": self.conditional_eprocess_verified,
            "scientific_promotion_gate_passed": self.scientific_promotion_gate_passed,
            "evidence_sha256": self.evidence_sha256,
            "promotion_authority": self.promotion_authority,
        }


class ForwardEconomicEvidenceAccumulator:
    """Prospective, append-only scientific evidence over one precommitted universe."""

    def __init__(self, protocol: ForwardEconomicProtocol) -> None:
        if type(protocol) is not ForwardEconomicProtocol:
            raise ForwardEconomicEvidenceError("protocol must be an exact ForwardEconomicProtocol")
        self._protocol = deepcopy(protocol)
        self._protocol_sha256 = self._protocol.identity_sha256
        self._steps: list[ForwardEconomicStep] = []
        self._absolute_log_e = Decimal(0)
        self._paired_log_e = Decimal(0)
        self._challenger_total = Decimal(0)
        self._champion_total = Decimal(0)
        self._challenger_peak = Decimal(0)
        self._challenger_max_drawdown = Decimal(0)

    def _validated_protocol(self) -> ForwardEconomicProtocol:
        if self._protocol.identity_sha256 != self._protocol_sha256:
            raise ForwardEconomicEvidenceError("internal protocol snapshot integrity drift")
        return self._protocol

    @property
    def protocol(self) -> ForwardEconomicProtocol:
        return deepcopy(self._validated_protocol())

    @property
    def steps(self) -> tuple[ForwardEconomicStep, ...]:
        self._validated_protocol()
        return tuple(deepcopy(step) for step in self._steps)

    @property
    def next_sequence(self) -> int:
        return self._validated_protocol().start_sequence + len(self._steps)

    def _resolver_authority_sha256(self, resolver: EconomicAuthorityResolver) -> str:
        try:
            authority_sha256 = resolver.authority_sha256
        except (AttributeError, TypeError) as exc:
            raise ForwardEconomicEvidenceError(
                "authority resolver must expose its canonical authority_sha256"
            ) from exc
        authority_sha256 = _sha256(authority_sha256, "resolver authority_sha256")
        if authority_sha256 != self.protocol.authority_binding_sha256:
            raise ForwardEconomicEvidenceError(
                "authority resolver does not match the frozen authority binding"
            )
        return authority_sha256

    def _resolve_exact(
        self,
        resolver: EconomicAuthorityResolver,
        *,
        policy_id: str,
        observation: ForwardDecisionObservation,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome:
        outcome = resolver.resolve(
            policy_id=policy_id,
            sequence=observation.sequence,
            universe_event_sha256=observation.universe_event_sha256,
            decision_sha256=decision_sha256,
        )
        if type(outcome) is not ResolvedPolicyOutcome:
            raise ForwardEconomicEvidenceError("authority resolver must return exact ResolvedPolicyOutcome")

        # Resolver-returned dataclasses are caller-owned.  frozen=True does not
        # prevent low-level mutation through object.__setattr__, and record()
        # resolves challenger and champion sequentially.  Snapshot and
        # reconstruct the exact outcome before validation so a later resolver
        # call cannot mutate an already-validated row before it is consumed.
        snapshot = deepcopy(outcome)
        outcome = ResolvedPolicyOutcome(
            policy_id=snapshot.policy_id,
            sequence=snapshot.sequence,
            universe_event_sha256=snapshot.universe_event_sha256,
            decision_sha256=snapshot.decision_sha256,
            decision_committed_at=snapshot.decision_committed_at,
            side=snapshot.side,
            accepted_odds=snapshot.accepted_odds,
            accepted_stake=snapshot.accepted_stake,
            net_pnl_currency=snapshot.net_pnl_currency,
            execution_evidence_sha256=snapshot.execution_evidence_sha256,
            execution_accepted_at=snapshot.execution_accepted_at,
            settlement_evidence_sha256=snapshot.settlement_evidence_sha256,
            settlement_available_at=snapshot.settlement_available_at,
            currency_code=snapshot.currency_code,
            denomination_authority_sha256=snapshot.denomination_authority_sha256,
            wager_pnl_currency=snapshot.wager_pnl_currency,
            economic_cost_currency=snapshot.economic_cost_currency,
            economic_cost_evidence_sha256=snapshot.economic_cost_evidence_sha256,
            economic_cost_incurred_at=snapshot.economic_cost_incurred_at,
            economic_cost_available_at=snapshot.economic_cost_available_at,
            economic_cost_causality=snapshot.economic_cost_causality,
            economic_cost_class_id=snapshot.economic_cost_class_id,
            economic_cost_allocation_authority_sha256=(
                snapshot.economic_cost_allocation_authority_sha256
            ),
        )
        if outcome.policy_id != policy_id:
            raise ForwardEconomicEvidenceError("resolved policy identity mismatch")
        if outcome.sequence != observation.sequence:
            raise ForwardEconomicEvidenceError("resolved sequence mismatch")
        if outcome.universe_event_sha256 != observation.universe_event_sha256:
            raise ForwardEconomicEvidenceError("resolved universe event mismatch")
        if outcome.decision_sha256 != decision_sha256:
            raise ForwardEconomicEvidenceError("resolved decision digest mismatch")
        protocol = self._validated_protocol()
        if outcome.decision_committed_at <= protocol.frozen_at:
            raise ForwardEconomicEvidenceError(
                "decision must causally follow frozen prospective protocol"
            )
        if protocol.currency_code is None:
            if (
                outcome.currency_code is not None
                or outcome.denomination_authority_sha256 is not None
            ):
                raise ForwardEconomicEvidenceError(
                    "resolved denomination cannot exceed the frozen protocol binding"
                )
        else:
            if outcome.currency_code != protocol.currency_code:
                raise ForwardEconomicEvidenceError(
                    "resolved currency does not match the frozen protocol denomination"
                )
            if (
                outcome.denomination_authority_sha256
                != protocol.denomination_authority_sha256
            ):
                raise ForwardEconomicEvidenceError(
                    "resolved denomination authority does not match the frozen protocol"
                )
        if (
            outcome.side is not BetSide.NONE
            and outcome.accepted_odds > protocol.maximum_accepted_odds
        ):
            raise ForwardEconomicEvidenceError("accepted odds exceed frozen protocol maximum")
        return outcome

    def _normalized_payoff_and_bounds(
        self,
        outcome: ResolvedPolicyOutcome,
    ) -> tuple[Decimal, Decimal, Decimal]:
        protocol = self.protocol
        risk = protocol.risk_unit_currency
        maximum_cost = protocol.maximum_economic_cost_currency
        cost = outcome.economic_cost_currency
        if cost > maximum_cost:
            raise ForwardEconomicEvidenceError(
                "resolved economic cost exceeds frozen protocol maximum"
            )
        with localcontext() as context:
            context.prec = _DECIMAL_PRECISION
            if outcome.side is BetSide.NONE:
                return (
                    +(outcome.net_pnl_currency / risk),
                    +(-maximum_cost / risk),
                    Decimal(0),
                )
            odds = outcome.accepted_odds
            stake = outcome.accepted_stake

            # Risk admission is money authority, not statistical arithmetic.
            # Compare exact finite-Decimal liability before rounded support
            # construction so sub-context LAY exposure cannot cross the cap.
            odds_fraction = Fraction(odds)
            stake_fraction = Fraction(stake)
            if outcome.side is BetSide.BACK:
                exposure_fraction = stake_fraction
                low_money = -stake
                high_money = (odds - Decimal(1)) * stake
            else:
                exposure_fraction = (odds_fraction - 1) * stake_fraction
                exposure = (odds - Decimal(1)) * stake
                low_money = -exposure
                high_money = stake
            if exposure_fraction > Fraction(risk):
                raise ForwardEconomicEvidenceError(
                    "accepted downside exposure exceeds fixed risk unit"
                )
            all_in_low_money = low_money - maximum_cost
            return (
                +(outcome.net_pnl_currency / risk),
                +(all_in_low_money / risk),
                +(high_money / risk),
            )

    def _validated_aggregate_state(
        self,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
        """Re-derive mutable caches from committed steps and fail closed on drift."""
        protocol = self._validated_protocol()
        absolute_log_e = Decimal(0)
        paired_log_e = Decimal(0)
        challenger_total = Decimal(0)
        champion_total = Decimal(0)
        challenger_peak = Decimal(0)
        challenger_max_drawdown = Decimal(0)
        expected_sequence = protocol.start_sequence

        for step in self._steps:
            if type(step) is not ForwardEconomicStep:
                raise ForwardEconomicEvidenceError(
                    "internal recorded step type integrity drift"
                )
            if step.sequence != expected_sequence:
                raise ForwardEconomicEvidenceError(
                    "internal recorded step sequence integrity drift"
                )
            expected_sequence += 1

            with localcontext() as context:
                context.prec = _DECIMAL_PRECISION
                absolute_log_e = +(
                    absolute_log_e
                    + _log_e_increment(
                        protocol.absolute_lambda,
                        step.challenger_normalized_pnl,
                        step.absolute_low,
                        step.absolute_high,
                    )
                )
                paired_log_e = +(
                    paired_log_e
                    + _log_e_increment(
                        protocol.paired_lambda,
                        step.paired_normalized_pnl,
                        step.paired_low,
                        step.paired_high,
                    )
                )

            if (
                step.absolute_log_e_after != absolute_log_e
                or step.paired_log_e_after != paired_log_e
            ):
                raise ForwardEconomicEvidenceError(
                    "recorded step statistical aggregate integrity drift"
                )

            challenger_total = _exact_decimal_sum(
                challenger_total,
                step.challenger_net_pnl_currency,
            )
            champion_total = _exact_decimal_sum(
                champion_total,
                step.champion_net_pnl_currency,
            )
            challenger_peak = max(challenger_peak, challenger_total)
            challenger_drawdown = _exact_decimal_sum(
                challenger_peak,
                challenger_total.copy_negate(),
            )
            challenger_max_drawdown = max(
                challenger_max_drawdown,
                challenger_drawdown,
            )

        cached = (
            self._absolute_log_e,
            self._paired_log_e,
            self._challenger_total,
            self._champion_total,
            self._challenger_peak,
            self._challenger_max_drawdown,
        )
        derived = (
            absolute_log_e,
            paired_log_e,
            challenger_total,
            champion_total,
            challenger_peak,
            challenger_max_drawdown,
        )
        if cached != derived:
            raise ForwardEconomicEvidenceError(
                "internal aggregate state integrity drift"
            )
        return derived

    def record(
        self,
        observation: ForwardDecisionObservation,
        resolver: EconomicAuthorityResolver,
    ) -> ForwardEconomicStep:
        if type(observation) is not ForwardDecisionObservation:
            raise ForwardEconomicEvidenceError("observation must be an exact ForwardDecisionObservation")

        # Snapshot the caller-owned observation before any resolver callback.
        # Reconstruct through the canonical validator so pre-call low-level
        # drift also cannot bypass sequence/universe/member identity checks.
        observation_snapshot = deepcopy(observation)
        observation = ForwardDecisionObservation(
            sequence=observation_snapshot.sequence,
            universe_sha256=observation_snapshot.universe_sha256,
            universe_event_sha256=observation_snapshot.universe_event_sha256,
            challenger_decision_sha256=observation_snapshot.challenger_decision_sha256,
            champion_decision_sha256=observation_snapshot.champion_decision_sha256,
        )
        if observation.sequence != self.next_sequence:
            raise ForwardEconomicEvidenceError("universe sequence must be contiguous and duplicate-free")
        if observation.universe_sha256 != self.protocol.universe_sha256:
            raise ForwardEconomicEvidenceError(
                "observation universe does not match the frozen universe commitment"
            )
        if any(
            step.universe_event_sha256 == observation.universe_event_sha256
            for step in self._steps
        ):
            raise ForwardEconomicEvidenceError(
                "frozen universe member cannot be counted more than once"
            )
        self._resolver_authority_sha256(resolver)
        (
            current_absolute_log_e,
            current_paired_log_e,
            current_challenger_total,
            current_champion_total,
            current_challenger_peak,
            current_challenger_max_drawdown,
        ) = self._validated_aggregate_state()

        challenger = self._resolve_exact(
            resolver,
            policy_id=self.protocol.challenger_id,
            observation=observation,
            decision_sha256=observation.challenger_decision_sha256,
        )
        champion = self._resolve_exact(
            resolver,
            policy_id=self.protocol.champion_id,
            observation=observation,
            decision_sha256=observation.champion_decision_sha256,
        )
        challenger_x, challenger_low, challenger_high = self._normalized_payoff_and_bounds(challenger)
        champion_x, champion_low, champion_high = self._normalized_payoff_and_bounds(champion)
        with localcontext() as context:
            context.prec = _DECIMAL_PRECISION
            paired_x = +(challenger_x - champion_x)
            paired_low = +(challenger_low - champion_high)
            paired_high = +(challenger_high - champion_low)
            new_absolute_log_e = +(
                current_absolute_log_e
                + _log_e_increment(
                    self.protocol.absolute_lambda,
                    challenger_x,
                    challenger_low,
                    challenger_high,
                )
            )
            new_paired_log_e = +(
                current_paired_log_e
                + _log_e_increment(
                    self.protocol.paired_lambda,
                    paired_x,
                    paired_low,
                    paired_high,
                )
            )

        # Currency totals/drawdown are exact finite-decimal identities, not
        # statistical approximations.  Never accumulate them under the bounded
        # e-process Decimal context.
        new_challenger_total = _exact_decimal_sum(
            current_challenger_total,
            challenger.net_pnl_currency,
        )
        new_champion_total = _exact_decimal_sum(
            current_champion_total,
            champion.net_pnl_currency,
        )
        new_peak = max(current_challenger_peak, new_challenger_total)
        new_drawdown = _exact_decimal_sum(
            new_peak,
            new_challenger_total.copy_negate(),
        )
        new_max_drawdown = max(current_challenger_max_drawdown, new_drawdown)

        step = ForwardEconomicStep(
            sequence=observation.sequence,
            universe_event_sha256=observation.universe_event_sha256,
            challenger_decision_sha256=observation.challenger_decision_sha256,
            champion_decision_sha256=observation.champion_decision_sha256,
            challenger_decision_committed_at=challenger.decision_committed_at,
            champion_decision_committed_at=champion.decision_committed_at,
            challenger_side=challenger.side,
            champion_side=champion.side,
            challenger_net_pnl_currency=challenger.net_pnl_currency,
            champion_net_pnl_currency=champion.net_pnl_currency,
            challenger_wager_pnl_currency=challenger.effective_wager_pnl_currency,
            champion_wager_pnl_currency=champion.effective_wager_pnl_currency,
            challenger_economic_cost_currency=challenger.economic_cost_currency,
            champion_economic_cost_currency=champion.economic_cost_currency,
            challenger_economic_cost_evidence_sha256=(
                challenger.economic_cost_evidence_sha256
            ),
            champion_economic_cost_evidence_sha256=(
                champion.economic_cost_evidence_sha256
            ),
            challenger_economic_cost_incurred_at=(
                challenger.economic_cost_incurred_at
            ),
            champion_economic_cost_incurred_at=(
                champion.economic_cost_incurred_at
            ),
            challenger_economic_cost_complete=challenger.economic_cost_complete,
            champion_economic_cost_complete=champion.economic_cost_complete,
            challenger_economic_cost_available_at=(
                challenger.economic_cost_available_at
            ),
            champion_economic_cost_available_at=(
                champion.economic_cost_available_at
            ),
            challenger_economic_available_at=challenger.economic_available_at,
            champion_economic_available_at=champion.economic_available_at,
            currency_code=self.protocol.currency_code,
            denomination_authority_sha256=(
                self.protocol.denomination_authority_sha256
            ),
            challenger_normalized_pnl=challenger_x,
            paired_normalized_pnl=paired_x,
            absolute_low=challenger_low,
            absolute_high=challenger_high,
            paired_low=paired_low,
            paired_high=paired_high,
            absolute_log_e_after=new_absolute_log_e,
            paired_log_e_after=new_paired_log_e,
            challenger_execution_evidence_sha256=challenger.execution_evidence_sha256,
            challenger_execution_accepted_at=challenger.execution_accepted_at,
            challenger_settlement_evidence_sha256=challenger.settlement_evidence_sha256,
            challenger_settlement_available_at=challenger.settlement_available_at,
            champion_execution_evidence_sha256=champion.execution_evidence_sha256,
            champion_execution_accepted_at=champion.execution_accepted_at,
            champion_settlement_evidence_sha256=champion.settlement_evidence_sha256,
            champion_settlement_available_at=champion.settlement_available_at,
            challenger_economic_cost_causality=challenger.economic_cost_causality,
            champion_economic_cost_causality=champion.economic_cost_causality,
            challenger_economic_cost_class_id=challenger.economic_cost_class_id,
            champion_economic_cost_class_id=champion.economic_cost_class_id,
            challenger_economic_cost_allocation_authority_sha256=(
                challenger.economic_cost_allocation_authority_sha256
            ),
            champion_economic_cost_allocation_authority_sha256=(
                champion.economic_cost_allocation_authority_sha256
            ),
        )

        self._steps.append(deepcopy(step))
        self._absolute_log_e = new_absolute_log_e
        self._paired_log_e = new_paired_log_e
        self._challenger_total = new_challenger_total
        self._champion_total = new_champion_total
        self._challenger_peak = new_peak
        self._challenger_max_drawdown = new_max_drawdown
        return deepcopy(step)

    def _realized_challenger_drawdown(self) -> tuple[Decimal, Decimal, bool]:
        capital_groups: dict[datetime, list[Decimal]] = {}
        for step in self._steps:
            cost = step.challenger_economic_cost_currency
            if cost != 0:
                cost_incurred_at = step.challenger_economic_cost_incurred_at
                if cost_incurred_at is None:
                    raise ForwardEconomicEvidenceError(
                        "challenger economic cost is missing causal incidence"
                    )
                capital_groups.setdefault(cost_incurred_at, []).append(
                    cost.copy_negate()
                )

            wager_pnl = step.challenger_wager_pnl_currency
            if wager_pnl != 0:
                if step.challenger_side is BetSide.NONE:
                    raise ForwardEconomicEvidenceError(
                        "NONE row cannot carry realized wager P&L"
                    )
                settlement_available_at = step.challenger_settlement_available_at
                if settlement_available_at is None:
                    raise ForwardEconomicEvidenceError(
                        "challenger wager P&L is missing settlement availability"
                    )
                capital_groups.setdefault(settlement_available_at, []).append(
                    wager_pnl
                )

        total = Decimal(0)
        peak = Decimal(0)
        maximum_drawdown = Decimal(0)
        chronology_unambiguous = True
        for available_at in sorted(capital_groups):
            pnls = capital_groups[available_at]
            positive = [pnl for pnl in pnls if pnl > 0]
            zero = [pnl for pnl in pnls if pnl == 0]
            negative = [pnl for pnl in pnls if pnl < 0]
            if positive and negative:
                chronology_unambiguous = False

            # Equal-time mixed-sign capital changes have no authoritative
            # intra-instant order. Positive-before-negative gives the
            # conservative drawdown envelope, while the gate remains
            # fail-closed because the exact chronology is ambiguous.
            for pnl in (*positive, *zero, *negative):
                total = _exact_decimal_sum(total, pnl)
                peak = max(peak, total)
                maximum_drawdown = max(
                    maximum_drawdown,
                    _exact_decimal_sum(peak, total.copy_negate()),
                )

        if total != self._challenger_total:
            raise ForwardEconomicEvidenceError(
                "causal capital deltas do not reconcile to all-in challenger P&L"
            )
        return peak, maximum_drawdown, chronology_unambiguous

    def summary(self) -> ForwardEconomicEvidenceSummary:
        protocol = self._validated_protocol()
        (
            absolute_log_e,
            paired_log_e,
            challenger_total,
            champion_total,
            _sequence_peak,
            _sequence_max_drawdown,
        ) = self._validated_aggregate_state()
        threshold = _log_threshold(protocol.challenger_alpha)
        minimum_events_satisfied = len(self._steps) >= protocol.minimum_events
        absolute_crossed = absolute_log_e >= threshold
        paired_crossed = paired_log_e >= threshold
        realized_peak, realized_max_drawdown, chronology_unambiguous = (
            self._realized_challenger_drawdown()
        )
        drawdown_passed = (
            chronology_unambiguous
            and realized_max_drawdown <= protocol.maximum_drawdown_currency
        )

        # The current resolver Protocol is only a structural integration seam:
        # any caller can implement it and repeat authority_binding_sha256.
        # Preserve such rows for audit/statistical diagnostics, but never let
        # them become positive scientific promotion evidence. This must become
        # True only after a product-owned resolver re-derives the exact
        # decision -> accepted execution -> terminal settlement -> net-PnL
        # chain from canonical durable/provider authorities.
        positive_authority_verified = False

        # The numerical accumulation uses a frozen lambda and a Hoeffding-style
        # bounded increment. That becomes an anytime-valid e-process only under
        # an exact conditional null relative to a prospectively frozen filtration
        # (including when bounds/actions become known before economic outcomes).
        # This module does not yet compose a product-owned authority proving those
        # conditional assumptions, so threshold crossing remains diagnostic only.
        conditional_eprocess_verified = False

        denomination_bound = (
            protocol.currency_code is not None
            and protocol.denomination_authority_sha256 is not None
        )
        all_in_economics_complete = bool(self._steps) and all(
            step.challenger_economic_cost_complete
            and step.champion_economic_cost_complete
            for step in self._steps
        )

        evidence_payload = {
            "schema_version": 1,
            "protocol_sha256": self._protocol_sha256,
            "steps": [step.to_payload() for step in self._steps],
        }
        evidence_sha256 = _canonical_digest(evidence_payload)
        return ForwardEconomicEvidenceSummary(
            protocol_sha256=self._protocol_sha256,
            currency_code=protocol.currency_code,
            denomination_authority_sha256=protocol.denomination_authority_sha256,
            denomination_bound=denomination_bound,
            all_in_economics_complete=all_in_economics_complete,
            observed_events=len(self._steps),
            next_sequence=self.next_sequence,
            challenger_total_pnl_currency=challenger_total,
            champion_total_pnl_currency=champion_total,
            challenger_peak_pnl_currency=realized_peak,
            challenger_max_drawdown_currency=realized_max_drawdown,
            absolute_log_e=absolute_log_e,
            paired_log_e=paired_log_e,
            log_threshold=threshold,
            absolute_threshold_crossed=absolute_crossed,
            paired_threshold_crossed=paired_crossed,
            minimum_events_satisfied=minimum_events_satisfied,
            drawdown_guard_passed=drawdown_passed,
            positive_authority_verified=positive_authority_verified,
            conditional_eprocess_verified=conditional_eprocess_verified,
            scientific_promotion_gate_passed=(
                positive_authority_verified
                and conditional_eprocess_verified
                and denomination_bound
                and all_in_economics_complete
                and minimum_events_satisfied
                and absolute_crossed
                and paired_crossed
                and drawdown_passed
            ),
            evidence_sha256=evidence_sha256,
            promotion_authority=False,
        )