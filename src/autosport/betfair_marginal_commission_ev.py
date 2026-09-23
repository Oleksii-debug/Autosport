"""Betfair market-level marginal ordinary-commission EV for exact evidence-bound inputs.

Pure consumer-side mathematics only. This module does not read Betfair, prove
accepted exposure, issue an effective commission rate, or authorize execution.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Context, Decimal, DecimalException, Inexact, InvalidOperation, Overflow, ROUND_HALF_UP, Underflow, localcontext
import hashlib
import json
import re

SCHEMA_VERSION = 1
SOURCE_FAMILY = "betfair.marginal-market-commission-ev.v1"
MAX_OUTCOMES = 512
MAX_SIGNIFICANT_DIGITS = 80
MAX_ADJUSTED_EXPONENT = 1000
COMMISSION_QUANTUM = Decimal("0.01")
COMMISSION_ROUNDING = "ROUND_HALF_UP"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_CTX = Context(prec=256, Emin=-999999, Emax=999999)
for _signal in (Inexact, InvalidOperation, Overflow, Underflow):
    _CTX.traps[_signal] = True


class BetfairMarginalCommissionEVError(ValueError):
    """Forward commission economics cannot be proven exactly."""


@dataclass(frozen=True, slots=True)
class BetfairMarketOutcomeEconomicInput:
    outcome_id: str
    probability: Decimal
    existing_gross_pnl: Decimal
    candidate_gross_pnl: Decimal

    def __post_init__(self) -> None:
        _text(self.outcome_id, "outcome_id")
        probability = _decimal(self.probability, "probability")
        if probability < 0 or probability > 1:
            raise BetfairMarginalCommissionEVError(
                "probability must be between 0 and 1 inclusive"
            )
        existing = _decimal(self.existing_gross_pnl, "existing_gross_pnl")
        candidate = _decimal(self.candidate_gross_pnl, "candidate_gross_pnl")
        if not _is_settlement_minor_unit(existing):
            raise BetfairMarginalCommissionEVError(
                "existing_gross_pnl must already reflect Betfair settlement rounding to 2 decimals"
            )
        if not _is_settlement_minor_unit(candidate):
            raise BetfairMarginalCommissionEVError(
                "candidate_gross_pnl must already reflect Betfair settlement rounding to 2 decimals"
            )


@dataclass(frozen=True, slots=True)
class BetfairOutcomeCommissionDelta:
    outcome_id: str
    probability: Decimal
    existing_gross_pnl: Decimal
    candidate_gross_pnl: Decimal
    combined_gross_pnl: Decimal
    base_after_commission_pnl: Decimal
    combined_after_commission_pnl: Decimal
    marginal_after_commission_pnl: Decimal


@dataclass(frozen=True, slots=True)
class BetfairMarginalCommissionEVProjection:
    account_id: str
    market_id: str
    currency: str
    effective_commission_rate: Decimal
    commission_quantum: Decimal
    commission_rounding: str
    probability_evidence_sha256: str
    exposure_evidence_sha256: str
    candidate_evidence_sha256: str
    commission_rate_evidence_sha256: str
    outcomes: tuple[BetfairOutcomeCommissionDelta, ...]
    gross_candidate_ev: Decimal
    marginal_after_commission_ev: Decimal
    candidate_standalone_after_commission_ev: Decimal
    calculation_sha256: str

    @property
    def decision_authorized(self) -> bool:
        return False


def calculate_betfair_marginal_commission_ev(
    *,
    account_id: str,
    market_id: str,
    currency: str,
    effective_commission_rate: Decimal,
    probability_evidence_sha256: str,
    exposure_evidence_sha256: str,
    candidate_evidence_sha256: str,
    commission_rate_evidence_sha256: str,
    outcomes: Sequence[BetfairMarketOutcomeEconomicInput],
) -> BetfairMarginalCommissionEVProjection:
    """Return marginal EV after ordinary market-level commission.

    The effective rate must already be authoritative for the exact account,
    market and decision snapshot. Gross P&L inputs must already incorporate
    Betfair's settlement rounding for constituent bet winnings/losses; this
    function then rounds each positive market commission charge to 2 decimals,
    half-up, before computing the marginal outcome delta. It deliberately does
    not infer a rate from Market Base Rate, Discount Rate, rewards, jurisdiction
    or defaults. SHA-256 fields bind evidence identity but do not validate it.
    """
    account = _text(account_id, "account_id")
    market = _text(market_id, "market_id")
    if type(currency) is not str or _CURRENCY_RE.fullmatch(currency) is None:
        raise BetfairMarginalCommissionEVError(
            "currency must be an uppercase three-letter code"
        )
    rate = _decimal(effective_commission_rate, "effective_commission_rate")
    if rate < 0 or rate > 1:
        raise BetfairMarginalCommissionEVError(
            "effective_commission_rate must be between 0 and 1 inclusive"
        )
    evidence = {
        "probability_evidence_sha256": _sha(probability_evidence_sha256, "probability_evidence_sha256"),
        "exposure_evidence_sha256": _sha(exposure_evidence_sha256, "exposure_evidence_sha256"),
        "candidate_evidence_sha256": _sha(candidate_evidence_sha256, "candidate_evidence_sha256"),
        "commission_rate_evidence_sha256": _sha(commission_rate_evidence_sha256, "commission_rate_evidence_sha256"),
    }
    if isinstance(outcomes, (str, bytes, bytearray)) or not isinstance(outcomes, Sequence):
        raise BetfairMarginalCommissionEVError("outcomes must be a finite sequence")
    snapshot = tuple(outcomes)
    if not snapshot or len(snapshot) > MAX_OUTCOMES:
        raise BetfairMarginalCommissionEVError(
            f"outcomes must contain between 1 and {MAX_OUTCOMES} items"
        )
    if any(type(item) is not BetfairMarketOutcomeEconomicInput for item in snapshot):
        raise BetfairMarginalCommissionEVError(
            "outcomes must contain exact BetfairMarketOutcomeEconomicInput values"
        )
    ordered = tuple(sorted(snapshot, key=lambda item: item.outcome_id))
    ids = tuple(item.outcome_id for item in ordered)
    if len(set(ids)) != len(ids):
        raise BetfairMarginalCommissionEVError("outcome_id values must be unique")

    try:
        with localcontext(_CTX) as context:
            context.clear_flags()
            if sum((item.probability for item in ordered), Decimal("0")) != Decimal("1"):
                raise BetfairMarginalCommissionEVError(
                    "outcome probabilities must sum exactly to 1"
                )
            rows: list[BetfairOutcomeCommissionDelta] = []
            gross_ev = Decimal("0")
            marginal_ev = Decimal("0")
            standalone_ev = Decimal("0")
            for item in ordered:
                combined = item.existing_gross_pnl + item.candidate_gross_pnl
                base_after = _net(item.existing_gross_pnl, rate)
                combined_after = _net(combined, rate)
                marginal = combined_after - base_after
                gross_ev += item.probability * item.candidate_gross_pnl
                marginal_ev += item.probability * marginal
                standalone_ev += item.probability * _net(item.candidate_gross_pnl, rate)
                rows.append(
                    BetfairOutcomeCommissionDelta(
                        item.outcome_id,
                        item.probability,
                        item.existing_gross_pnl,
                        item.candidate_gross_pnl,
                        combined,
                        base_after,
                        combined_after,
                        marginal,
                    )
                )
    except DecimalException as exc:
        raise BetfairMarginalCommissionEVError(
            "commission EV arithmetic exceeded the exact Decimal envelope"
        ) from exc

    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_family": SOURCE_FAMILY,
        "account_id": account,
        "market_id": market,
        "currency": currency,
        "effective_commission_rate": _decimal_text(rate),
        "commission_quantum": _decimal_text(COMMISSION_QUANTUM),
        "commission_rounding": COMMISSION_ROUNDING,
        **evidence,
        "outcomes": [
            {
                "outcome_id": item.outcome_id,
                "probability": _decimal_text(item.probability),
                "existing_gross_pnl": _decimal_text(item.existing_gross_pnl),
                "candidate_gross_pnl": _decimal_text(item.candidate_gross_pnl),
            }
            for item in ordered
        ],
        "gross_candidate_ev": _decimal_text(gross_ev),
        "marginal_after_commission_ev": _decimal_text(marginal_ev),
        "candidate_standalone_after_commission_ev": _decimal_text(standalone_ev),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return BetfairMarginalCommissionEVProjection(
        account,
        market,
        currency,
        rate,
        COMMISSION_QUANTUM,
        COMMISSION_ROUNDING,
        evidence["probability_evidence_sha256"],
        evidence["exposure_evidence_sha256"],
        evidence["candidate_evidence_sha256"],
        evidence["commission_rate_evidence_sha256"],
        tuple(rows),
        gross_ev,
        marginal_ev,
        standalone_ev,
        digest,
    )


def _net(gross_pnl: Decimal, rate: Decimal) -> Decimal:
    if gross_pnl <= 0:
        return gross_pnl
    unrounded_charge = gross_pnl * rate
    with localcontext(_CTX) as rounding_context:
        rounding_context.traps[Inexact] = False
        charge = unrounded_charge.quantize(
            COMMISSION_QUANTUM,
            rounding=ROUND_HALF_UP,
            context=rounding_context,
        )
    return gross_pnl - charge


def _is_settlement_minor_unit(value: Decimal) -> bool:
    if value == 0:
        return True
    _, digits, exponent = value.as_tuple()
    trailing_zeroes = 0
    for digit in reversed(digits):
        if digit != 0:
            break
        trailing_zeroes += 1
    return exponent + trailing_zeroes >= -2


def _decimal(value: object, label: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise BetfairMarginalCommissionEVError(f"{label} must be a finite Decimal")
    _, digits, exponent = value.as_tuple()
    adjusted = value.adjusted() if value != 0 else 0
    if len(digits) > MAX_SIGNIFICANT_DIGITS or abs(adjusted) > MAX_ADJUSTED_EXPONENT:
        raise BetfairMarginalCommissionEVError(f"{label} exceeds the bounded Decimal envelope")
    if not isinstance(exponent, int):
        raise BetfairMarginalCommissionEVError(f"{label} must have a finite exponent")
    return value


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairMarginalCommissionEVError(f"{label} must be non-empty canonical text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BetfairMarginalCommissionEVError(
            f"{label} must be valid UTF-8 canonical text"
        ) from exc
    if (
        len(value) > 256
        or len(encoded) > 1024
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise BetfairMarginalCommissionEVError(f"{label} is not bounded canonical text")
    return value


def _sha(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise BetfairMarginalCommissionEVError(f"{label} must be lowercase SHA-256")
    return value


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    sign, digits, exponent = value.as_tuple()
    coefficient = list(digits)
    while coefficient and coefficient[-1] == 0:
        coefficient.pop()
        exponent += 1
    body = "".join(str(digit) for digit in coefficient)
    prefix = "-" if sign else ""
    return f"{prefix}{body}" if exponent == 0 else f"{prefix}{body}e{exponent}"
