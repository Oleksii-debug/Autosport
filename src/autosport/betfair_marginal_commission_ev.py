"""Betfair market-level marginal ordinary-commission EV for exact evidence-bound inputs.

Pure consumer-side mathematics only. This module does not read Betfair, prove
accepted exposure, issue an effective commission rate, or authorize execution.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
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
COMMISSION_RATE_BASIS = "DECISION_SNAPSHOT_CONDITIONAL"
PROVIDER_APPLICABILITY_PROVEN = False
PROVIDER_POSTED_EXACT = False
SETTLEMENT_RATE_AUTHORITATIVE = False
EXECUTION_AUTHORIZED = False
REAL_MONEY_EXECUTION = False
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
    effective_commission_rate: Decimal = field(repr=False)
    combined_gross_pnl: Decimal = field(init=False)
    base_after_commission_pnl: Decimal = field(init=False)
    combined_after_commission_pnl: Decimal = field(init=False)
    marginal_after_commission_pnl: Decimal = field(init=False)

    def __post_init__(self) -> None:
        BetfairMarketOutcomeEconomicInput(
            self.outcome_id,
            self.probability,
            self.existing_gross_pnl,
            self.candidate_gross_pnl,
        )
        rate = _decimal(
            self.effective_commission_rate,
            "effective_commission_rate",
        )
        if rate < 0 or rate > 1:
            raise BetfairMarginalCommissionEVError(
                "effective_commission_rate must be between 0 and 1 inclusive"
            )
        try:
            with localcontext(_CTX):
                combined = self.existing_gross_pnl + self.candidate_gross_pnl
                base_after = _net(self.existing_gross_pnl, rate)
                combined_after = _net(combined, rate)
                marginal = combined_after - base_after
        except DecimalException as exc:
            raise BetfairMarginalCommissionEVError(
                "commission EV arithmetic exceeded the exact Decimal envelope"
            ) from exc
        object.__setattr__(self, "combined_gross_pnl", combined)
        object.__setattr__(self, "base_after_commission_pnl", base_after)
        object.__setattr__(self, "combined_after_commission_pnl", combined_after)
        object.__setattr__(self, "marginal_after_commission_pnl", marginal)


@dataclass(frozen=True, slots=True)
class BetfairMarginalCommissionEVProjection:
    account_id: str
    market_id: str
    currency: str
    effective_commission_rate: Decimal
    probability_evidence_sha256: str
    exposure_evidence_sha256: str
    candidate_evidence_sha256: str
    commission_rate_evidence_sha256: str
    outcomes: tuple[BetfairOutcomeCommissionDelta, ...]
    commission_quantum: Decimal = field(init=False)
    commission_rounding: str = field(init=False)
    gross_candidate_ev: Decimal = field(init=False)
    marginal_after_commission_ev: Decimal = field(init=False)
    candidate_standalone_after_commission_ev: Decimal = field(init=False)
    calculation_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        account = _text(self.account_id, "account_id")
        market = _text(self.market_id, "market_id")
        if type(self.currency) is not str or _CURRENCY_RE.fullmatch(self.currency) is None:
            raise BetfairMarginalCommissionEVError(
                "currency must be an uppercase three-letter code"
            )
        rate = _decimal(
            self.effective_commission_rate,
            "effective_commission_rate",
        )
        if rate < 0 or rate > 1:
            raise BetfairMarginalCommissionEVError(
                "effective_commission_rate must be between 0 and 1 inclusive"
            )
        evidence = {
            "probability_evidence_sha256": _sha(
                self.probability_evidence_sha256,
                "probability_evidence_sha256",
            ),
            "exposure_evidence_sha256": _sha(
                self.exposure_evidence_sha256,
                "exposure_evidence_sha256",
            ),
            "candidate_evidence_sha256": _sha(
                self.candidate_evidence_sha256,
                "candidate_evidence_sha256",
            ),
            "commission_rate_evidence_sha256": _sha(
                self.commission_rate_evidence_sha256,
                "commission_rate_evidence_sha256",
            ),
        }
        if type(self.outcomes) is not tuple or not self.outcomes or len(self.outcomes) > MAX_OUTCOMES:
            raise BetfairMarginalCommissionEVError(
                f"outcomes must contain between 1 and {MAX_OUTCOMES} items"
            )
        if any(type(row) is not BetfairOutcomeCommissionDelta for row in self.outcomes):
            raise BetfairMarginalCommissionEVError(
                "outcomes must contain exact BetfairOutcomeCommissionDelta values"
            )
        canonical_rows = tuple(
            sorted(
                (
                    BetfairOutcomeCommissionDelta(
                        row.outcome_id,
                        row.probability,
                        row.existing_gross_pnl,
                        row.candidate_gross_pnl,
                        rate,
                    )
                    for row in self.outcomes
                ),
                key=lambda row: row.outcome_id,
            )
        )
        ids = tuple(row.outcome_id for row in canonical_rows)
        if len(set(ids)) != len(ids):
            raise BetfairMarginalCommissionEVError(
                "outcome_id values must be unique"
            )
        try:
            with localcontext(_CTX) as context:
                context.clear_flags()
                if sum(
                    (row.probability for row in canonical_rows),
                    Decimal("0"),
                ) != Decimal("1"):
                    raise BetfairMarginalCommissionEVError(
                        "outcome probabilities must sum exactly to 1"
                    )
                gross_ev = sum(
                    (
                        row.probability * row.candidate_gross_pnl
                        for row in canonical_rows
                    ),
                    Decimal("0"),
                )
                marginal_ev = sum(
                    (
                        row.probability * row.marginal_after_commission_pnl
                        for row in canonical_rows
                    ),
                    Decimal("0"),
                )
                standalone_ev = sum(
                    (
                        row.probability
                        * _net(row.candidate_gross_pnl, rate)
                        for row in canonical_rows
                    ),
                    Decimal("0"),
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
            "currency": self.currency,
            "effective_commission_rate": _decimal_text(rate),
            "commission_rate_basis": COMMISSION_RATE_BASIS,
            "commission_quantum": _decimal_text(COMMISSION_QUANTUM),
            "commission_rounding": COMMISSION_ROUNDING,
            "provider_applicability_proven": PROVIDER_APPLICABILITY_PROVEN,
            "provider_posted_exact": PROVIDER_POSTED_EXACT,
            "settlement_rate_authoritative": SETTLEMENT_RATE_AUTHORITATIVE,
            "execution_authorized": EXECUTION_AUTHORIZED,
            "real_money_execution": REAL_MONEY_EXECUTION,
            "decision_authorized": False,
            **evidence,
            "outcomes": [
                {
                    "outcome_id": row.outcome_id,
                    "probability": _decimal_text(row.probability),
                    "existing_gross_pnl": _decimal_text(row.existing_gross_pnl),
                    "candidate_gross_pnl": _decimal_text(row.candidate_gross_pnl),
                }
                for row in canonical_rows
            ],
            "gross_candidate_ev": _decimal_text(gross_ev),
            "marginal_after_commission_ev": _decimal_text(marginal_ev),
            "candidate_standalone_after_commission_ev": _decimal_text(
                standalone_ev
            ),
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

        object.__setattr__(self, "outcomes", canonical_rows)
        object.__setattr__(self, "commission_quantum", COMMISSION_QUANTUM)
        object.__setattr__(self, "commission_rounding", COMMISSION_ROUNDING)
        object.__setattr__(self, "gross_candidate_ev", gross_ev)
        object.__setattr__(
            self,
            "marginal_after_commission_ev",
            marginal_ev,
        )
        object.__setattr__(
            self,
            "candidate_standalone_after_commission_ev",
            standalone_ev,
        )
        object.__setattr__(self, "calculation_sha256", digest)

    @property
    def commission_rate_basis(self) -> str:
        return COMMISSION_RATE_BASIS

    @property
    def provider_applicability_proven(self) -> bool:
        return PROVIDER_APPLICABILITY_PROVEN

    @property
    def provider_posted_exact(self) -> bool:
        return PROVIDER_POSTED_EXACT

    @property
    def settlement_rate_authoritative(self) -> bool:
        return SETTLEMENT_RATE_AUTHORITATIVE

    @property
    def execution_authorized(self) -> bool:
        return EXECUTION_AUTHORIZED

    @property
    def real_money_execution(self) -> bool:
        return REAL_MONEY_EXECUTION

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
    """Return rate-conditioned marginal EV after ordinary market-level commission.

    The effective rate must already be authoritative for the exact account,
    market and decision snapshot. That does not make the rate authoritative at
    eventual market settlement: this pure projection therefore exposes
    settlement_rate_authoritative=false, provider_applicability_proven=false
    and provider_posted_exact=false. Gross P&L inputs must already incorporate
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

    rows = tuple(
        BetfairOutcomeCommissionDelta(
            item.outcome_id,
            item.probability,
            item.existing_gross_pnl,
            item.candidate_gross_pnl,
            rate,
        )
        for item in ordered
    )
    return BetfairMarginalCommissionEVProjection(
        account,
        market,
        currency,
        rate,
        evidence["probability_evidence_sha256"],
        evidence["exposure_evidence_sha256"],
        evidence["candidate_evidence_sha256"],
        evidence["commission_rate_evidence_sha256"],
        rows,
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
