"""Conservative mixed-outcome capital reservation for a Betfair instruction batch.

This module composes two existing Autosport authorities without replacing them:

* ``betfair_order_liability`` owns exact requested-order reserve arithmetic.
* ``betfair_live_capital_at_risk`` owns provider-backed current/cleared risk truth.

A batch member retains its full requested reserve unless canonical live-risk
evidence proves a smaller non-negative current exposure.  Members are summed
independently; profit, failure, or zero exposure on one member never offsets
another member's reserve.

The result is risk evidence only.  It grants no provider-write, execution,
settlement, routing, readiness, or real-money authority.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction

from .betfair_live_capital_at_risk import (
    BetfairLiveCapitalAtRiskError,
    BetfairLiveCapitalAtRiskEvidence,
    BetfairLiveCapitalAtRiskTruth,
)
from .betfair_order_liability import (
    BetfairOrderLiabilityError,
    BetfairOrderReserve,
)


class BetfairBatchCapitalReservationError(ValueError):
    """Fail-closed validation error for batch capital reservation."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairBatchCapitalReservationError(
            f"{name} must be non-empty canonical text"
        )
    return value


def _instant(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BetfairBatchCapitalReservationError(
            f"{name} must be a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _instant_text(value: datetime) -> str:
    return _instant(value, "instant").isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise BetfairBatchCapitalReservationError(
            "decimal value must be a finite Decimal"
        )
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _fraction_payload(value: Fraction | None) -> object:
    if value is None:
        return None
    if type(value) is not Fraction:
        raise BetfairBatchCapitalReservationError(
            "reserve rational value must be an exact Fraction"
        )
    return {"numerator": value.numerator, "denominator": value.denominator}


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fraction_to_decimal_exact(value: Fraction) -> Decimal:
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise BetfairBatchCapitalReservationError(
            "batch reserve sum is not an exact finite decimal"
        )
    scale = max(twos, fives)
    scaled = (
        value.numerator
        * (2 ** (scale - twos))
        * (5 ** (scale - fives))
    )
    if scaled == 0:
        return Decimal(0)
    sign = int(scaled < 0)
    digits = tuple(int(char) for char in str(abs(scaled)))
    return Decimal((sign, digits, -scale))


def _exact_decimal_sum(values: tuple[Decimal, ...]) -> Decimal:
    total = Fraction(0)
    for index, value in enumerate(values):
        if type(value) is not Decimal or not value.is_finite():
            raise BetfairBatchCapitalReservationError(
                f"reserve[{index}] must be a finite Decimal"
            )
        total += Fraction(value)
    return _fraction_to_decimal_exact(total)


def _validated_reserve(value: object) -> BetfairOrderReserve:
    if type(value) is not BetfairOrderReserve:
        raise BetfairBatchCapitalReservationError(
            "requested_reserve must be an exact BetfairOrderReserve"
        )
    try:
        # Re-run the canonical self-validation on every consumption.  This also
        # detects trusted-process mutation performed via object.__setattr__.
        value.__post_init__()
    except (BetfairOrderLiabilityError, TypeError, ValueError, ArithmeticError) as exc:
        raise BetfairBatchCapitalReservationError(
            "requested reserve no longer matches canonical liability derivation"
        ) from exc
    return value


def _reserve_identity(value: BetfairOrderReserve) -> str:
    reserve = _validated_reserve(value)
    return _canonical_digest(
        {
            "schema": "autosport.betfair_order_reserve.binding",
            "schema_version": 1,
            "side": reserve.side.value,
            "market_betting_type": reserve.market_betting_type.value,
            "order_type": reserve.order_type.value,
            "price": None if reserve.price is None else _decimal_text(reserve.price),
            "size": None if reserve.size is None else _decimal_text(reserve.size),
            "liability": (
                None if reserve.liability is None else _decimal_text(reserve.liability)
            ),
            "target_type": (
                None if reserve.target_type is None else reserve.target_type.value
            ),
            "target_size": (
                None
                if reserve.target_size is None
                else _decimal_text(reserve.target_size)
            ),
            "currency_quantum": (
                None
                if reserve.currency_quantum is None
                else _decimal_text(reserve.currency_quantum)
            ),
            "each_way": reserve.each_way,
            "reserve": _decimal_text(reserve.reserve),
            "raw_reserve": _fraction_payload(reserve.raw_reserve),
            "backer_stake_equivalent": _fraction_payload(
                reserve.backer_stake_equivalent
            ),
            "execution_authority": False,
        }
    )


def _validated_live_evidence(
    value: object,
    *,
    attempt_id: str,
    action_id: str,
) -> BetfairLiveCapitalAtRiskEvidence:
    if type(value) is not BetfairLiveCapitalAtRiskEvidence:
        raise BetfairBatchCapitalReservationError(
            "live_risk_evidence must be exact BetfairLiveCapitalAtRiskEvidence"
        )
    try:
        value.assert_authoritative()
    except (BetfairLiveCapitalAtRiskError, TypeError, ValueError, ArithmeticError) as exc:
        raise BetfairBatchCapitalReservationError(
            "live risk evidence is not current canonical provider evidence"
        ) from exc
    if value.attempt_id != attempt_id or value.action_id != action_id:
        raise BetfairBatchCapitalReservationError(
            "live risk evidence identity does not match batch member"
        )
    return value


@dataclass(frozen=True, slots=True)
class BetfairBatchInstructionReservation:
    """One independently reserved Betfair instruction.

    ``live_risk_evidence=None`` means there is no canonical provider-backed
    reduction for this member, so its complete requested reserve is retained.
    UNKNOWN evidence is also conservative and retains the full reserve.
    """

    instruction_id: str
    action_id: str
    attempt_id: str
    requested_reserve: BetfairOrderReserve
    live_risk_evidence: BetfairLiveCapitalAtRiskEvidence | None = None

    def __post_init__(self) -> None:
        _text(self.instruction_id, "instruction_id")
        _text(self.action_id, "action_id")
        _text(self.attempt_id, "attempt_id")
        _validated_reserve(self.requested_reserve)
        if self.live_risk_evidence is not None:
            _validated_live_evidence(
                self.live_risk_evidence,
                attempt_id=self.attempt_id,
                action_id=self.action_id,
            )
            # Force the economic relation check at construction as well as on
            # every later read.
            _ = self.effective_reserve

    @property
    def requested_reserve_id(self) -> str:
        return _reserve_identity(self.requested_reserve)

    @property
    def effective_reserve(self) -> Decimal:
        requested = _validated_reserve(self.requested_reserve).reserve
        evidence = self.live_risk_evidence
        if evidence is None:
            return requested

        evidence = _validated_live_evidence(
            evidence,
            attempt_id=self.attempt_id,
            action_id=self.action_id,
        )
        if evidence.truth is BetfairLiveCapitalAtRiskTruth.UNKNOWN:
            return requested
        if evidence.truth is not BetfairLiveCapitalAtRiskTruth.EXACT:
            raise BetfairBatchCapitalReservationError(
                "unsupported live-risk truth state"
            )
        current = evidence.capital_at_risk
        if (
            type(current) is not Decimal
            or not current.is_finite()
            or current < 0
            or current > requested
        ):
            raise BetfairBatchCapitalReservationError(
                "provider-backed current risk exceeds requested reserve bounds"
            )
        return current

    @property
    def provider_risk_exact(self) -> bool:
        evidence = self.live_risk_evidence
        if evidence is None:
            return False
        evidence = _validated_live_evidence(
            evidence,
            attempt_id=self.attempt_id,
            action_id=self.action_id,
        )
        # effective_reserve performs all amount/bound checks.
        _ = self.effective_reserve
        return evidence.truth is BetfairLiveCapitalAtRiskTruth.EXACT

    def to_payload(self) -> dict[str, object]:
        evidence = self.live_risk_evidence
        if evidence is not None:
            evidence = _validated_live_evidence(
                evidence,
                attempt_id=self.attempt_id,
                action_id=self.action_id,
            )
        return {
            "instruction_id": self.instruction_id,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "requested_reserve_id": self.requested_reserve_id,
            "requested_reserve": _decimal_text(
                _validated_reserve(self.requested_reserve).reserve
            ),
            "live_risk_evidence_id": (
                None if evidence is None else evidence.evidence_id
            ),
            "provider_risk_exact": self.provider_risk_exact,
            "effective_reserve": _decimal_text(self.effective_reserve),
        }


@dataclass(frozen=True, slots=True)
class BetfairBatchCapitalReservation:
    """Content-addressed conservative reserve across an ordered instruction set."""

    batch_id: str
    members: tuple[BetfairBatchInstructionReservation, ...]
    as_of: datetime
    execution_authority: bool = False

    def __post_init__(self) -> None:
        _text(self.batch_id, "batch_id")
        if (
            type(self.members) is not tuple
            or not self.members
            or any(type(item) is not BetfairBatchInstructionReservation for item in self.members)
        ):
            raise BetfairBatchCapitalReservationError(
                "members must be a non-empty tuple of exact batch reservations"
            )
        _instant(self.as_of, "as_of")
        if self.execution_authority is not False:
            raise BetfairBatchCapitalReservationError(
                "batch capital reservation never grants execution authority"
            )

        instruction_ids = tuple(item.instruction_id for item in self.members)
        action_ids = tuple(item.action_id for item in self.members)
        attempt_ids = tuple(item.attempt_id for item in self.members)
        if len(set(instruction_ids)) != len(instruction_ids):
            raise BetfairBatchCapitalReservationError(
                "batch instruction identities must be unique"
            )
        if len(set(action_ids)) != len(action_ids):
            raise BetfairBatchCapitalReservationError(
                "batch action identities must be unique"
            )
        if len(set(attempt_ids)) != len(attempt_ids):
            raise BetfairBatchCapitalReservationError(
                "batch attempt identities must be unique"
            )
        # Validate every member against current canonical evidence at creation.
        _ = self.total_reserved
        _ = self.evidence_id

    @property
    def total_requested_reserve(self) -> Decimal:
        return _exact_decimal_sum(
            tuple(
                _validated_reserve(member.requested_reserve).reserve
                for member in self.members
            )
        )

    @property
    def total_reserved(self) -> Decimal:
        return _exact_decimal_sum(
            tuple(member.effective_reserve for member in self.members)
        )

    @property
    def all_provider_risk_exact(self) -> bool:
        return all(member.provider_risk_exact for member in self.members)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.betfair_batch_capital_reservation",
            "schema_version": 1,
            "batch_id": self.batch_id,
            "as_of": _instant_text(self.as_of),
            "members": [member.to_payload() for member in self.members],
            "total_requested_reserve": _decimal_text(self.total_requested_reserve),
            "total_reserved": _decimal_text(self.total_reserved),
            "all_provider_risk_exact": self.all_provider_risk_exact,
            "cross_member_netting": False,
            "execution_authority": False,
        }

    @property
    def evidence_id(self) -> str:
        return _canonical_digest(self.to_payload())
