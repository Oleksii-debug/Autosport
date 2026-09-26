"""Fail-closed uncertainty-aware sizing prerequisite.

The contract consumes externally produced probability-interval and all-in payoff
evidence. It does not estimate probabilities, costs, slippage, or settlement rules;
it does not place tickets and it does not replace :mod:`autosport.risk`.  An
``ELIGIBLE`` result is only a conservative stake ceiling that downstream risk and
execution authorities may tighten or reject.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Context, Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from enum import Enum
from typing import Final


_SCHEMA: Final = "autosport.uncertainty_sizing_evidence"
_SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")
_SIZING_ARITHMETIC_PRECISION: Final = 80
_SIZING_METHOD_ID: Final = "lower-bound-fractional-kelly-v1"
_EVIDENCE_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "evidence_id",
        "candidate_id",
        "quote_sha256",
        "probability_model_version_id",
        "calibration_bundle_sha256",
        "causal_cutoff",
        "produced_at",
        "valid_until",
        "probability_lower",
        "probability_point",
        "probability_upper",
        "net_win_profit_per_stake",
        "evidence_refs",
    }
)


class UncertaintySizingError(ValueError):
    """Raised when uncertainty/sizing evidence is non-canonical."""


class SizingAction(str, Enum):
    ABSTAIN = "abstain"
    ELIGIBLE = "eligible"


def _text(name: str, value: object, *, maximum_bytes: int = 512) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise UncertaintySizingError(f"{name} must be non-empty canonical text")
    if "\x00" in value:
        raise UncertaintySizingError(f"{name} must not contain NUL")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise UncertaintySizingError(f"{name} must be valid UTF-8 text") from exc
    if len(encoded) > maximum_bytes:
        raise UncertaintySizingError(
            f"{name} must be at most {maximum_bytes} UTF-8 bytes"
        )
    return value


def _sha256(name: str, value: object) -> str:
    digest = _text(name, value, maximum_bytes=64)
    if len(digest) != 64 or any(character not in _HEX for character in digest):
        raise UncertaintySizingError(f"{name} must be lowercase SHA-256 hex")
    return digest


def _decimal(name: str, value: object) -> Decimal:
    # Risk/economic ingress must not dispatch through caller-defined Decimal
    # subclasses before the sizing authority has accepted the value.
    if type(value) is not Decimal:
        raise UncertaintySizingError(f"{name} must be a finite exact Decimal")
    if not value.is_finite():
        raise UncertaintySizingError(f"{name} must be a finite exact Decimal")
    return value


def _utc(name: str, value: object) -> tuple[str, datetime]:
    text = _text(name, value, maximum_bytes=64)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise UncertaintySizingError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UncertaintySizingError(f"{name} must be timezone-aware")
    if parsed.utcoffset() != timedelta(0):
        raise UncertaintySizingError(f"{name} must use UTC +00:00")
    if parsed.isoformat() != text:
        raise UncertaintySizingError(
            f"{name} must use datetime.isoformat() canonical UTC form"
        )
    return text, parsed


def _refs(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise UncertaintySizingError("evidence_refs must be a tuple")
    refs = tuple(_text("evidence_ref", item, maximum_bytes=1024) for item in value)
    if not refs:
        raise UncertaintySizingError("evidence_refs must not be empty")
    if refs != tuple(sorted(set(refs))):
        raise UncertaintySizingError("evidence_refs must be sorted and unique")
    return refs


def _decimal_from_json(name: str, value: object) -> Decimal:
    if type(value) is not str:
        raise UncertaintySizingError(f"{name} must be a canonical decimal JSON string")
    _text(name, value, maximum_bytes=128)
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise UncertaintySizingError(f"{name} must be a canonical decimal JSON string") from exc
    if not parsed.is_finite() or str(parsed) != value:
        raise UncertaintySizingError(f"{name} must be a canonical decimal JSON string")
    return parsed


@dataclass(frozen=True, slots=True)
class UncertaintySizingEvidence:
    """Externally produced evidence for one exact candidate and executable quote.

    ``net_win_profit_per_stake`` is the all-in *net* profit on a winning unit stake,
    after whatever cost/slippage assumptions the evidence producer explicitly bound.
    Loss payoff is conservatively fixed at -1 unit of stake. This object does not
    derive either payoff or probability.
    """

    evidence_id: str
    candidate_id: str
    quote_sha256: str
    probability_model_version_id: str
    calibration_bundle_sha256: str
    causal_cutoff: str
    produced_at: str
    valid_until: str
    probability_lower: Decimal
    probability_point: Decimal
    probability_upper: Decimal
    net_win_profit_per_stake: Decimal
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _text("evidence_id", self.evidence_id, maximum_bytes=160)
        _text("candidate_id", self.candidate_id, maximum_bytes=256)
        _sha256("quote_sha256", self.quote_sha256)
        _text(
            "probability_model_version_id",
            self.probability_model_version_id,
            maximum_bytes=256,
        )
        _sha256("calibration_bundle_sha256", self.calibration_bundle_sha256)
        _, cutoff = _utc("causal_cutoff", self.causal_cutoff)
        _, produced = _utc("produced_at", self.produced_at)
        _, valid_until = _utc("valid_until", self.valid_until)
        if cutoff > produced:
            raise UncertaintySizingError("causal_cutoff must not exceed produced_at")
        if valid_until <= produced:
            raise UncertaintySizingError("valid_until must be strictly after produced_at")

        lower = _decimal("probability_lower", self.probability_lower)
        point = _decimal("probability_point", self.probability_point)
        upper = _decimal("probability_upper", self.probability_upper)
        if not (Decimal("0") <= lower <= point <= upper <= Decimal("1")):
            raise UncertaintySizingError(
                "probability interval must satisfy 0 <= lower <= point <= upper <= 1"
            )
        payoff = _decimal("net_win_profit_per_stake", self.net_win_profit_per_stake)
        if payoff <= 0:
            raise UncertaintySizingError(
                "net_win_profit_per_stake must be strictly positive"
            )
        _refs(self.evidence_refs)

    @property
    def uncertainty_width(self) -> Decimal:
        # Width is an upper-risk quantity: if finite context precision ever rounds,
        # round upward so uncertainty cannot be understated.
        with localcontext(
            Context(prec=_SIZING_ARITHMETIC_PRECISION, rounding=ROUND_CEILING)
        ):
            return +(self.probability_upper - self.probability_lower)

    @property
    def conservative_ev_per_stake(self) -> Decimal:
        # Win: +b. Loss: -1. Use lower probability bound, never the point estimate.
        # Use a private fixed context so caller-global Decimal settings cannot
        # change a sizing decision. ROUND_FLOOR is conservative for positive EV.
        p = self.probability_lower
        b = self.net_win_profit_per_stake
        with localcontext(
            Context(prec=_SIZING_ARITHMETIC_PRECISION, rounding=ROUND_FLOOR)
        ):
            return +(p * b - (Decimal("1") - p))

    @property
    def conservative_full_kelly_fraction(self) -> Decimal:
        edge = self.conservative_ev_per_stake
        if edge <= 0:
            return Decimal("0")
        with localcontext(
            Context(prec=_SIZING_ARITHMETIC_PRECISION, rounding=ROUND_FLOOR)
        ):
            return +(edge / self.net_win_profit_per_stake)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "evidence_id": self.evidence_id,
            "candidate_id": self.candidate_id,
            "quote_sha256": self.quote_sha256,
            "probability_model_version_id": self.probability_model_version_id,
            "calibration_bundle_sha256": self.calibration_bundle_sha256,
            "causal_cutoff": self.causal_cutoff,
            "produced_at": self.produced_at,
            "valid_until": self.valid_until,
            "probability_lower": str(self.probability_lower),
            "probability_point": str(self.probability_point),
            "probability_upper": str(self.probability_upper),
            "net_win_profit_per_stake": str(self.net_win_profit_per_stake),
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "UncertaintySizingEvidence":
        if type(raw) is not dict or set(raw) != _EVIDENCE_KEYS:
            raise UncertaintySizingError(
                "uncertainty sizing evidence must contain exactly canonical fields"
            )
        if (
            type(raw["schema"]) is not str
            or raw["schema"] != _SCHEMA
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != _SCHEMA_VERSION
        ):
            raise UncertaintySizingError("unsupported uncertainty sizing schema")
        refs = raw["evidence_refs"]
        if type(refs) is not list or any(type(item) is not str for item in refs):
            raise UncertaintySizingError("evidence_refs must be a JSON string array")
        return cls(
            evidence_id=raw["evidence_id"],
            candidate_id=raw["candidate_id"],
            quote_sha256=raw["quote_sha256"],
            probability_model_version_id=raw["probability_model_version_id"],
            calibration_bundle_sha256=raw["calibration_bundle_sha256"],
            causal_cutoff=raw["causal_cutoff"],
            produced_at=raw["produced_at"],
            valid_until=raw["valid_until"],
            probability_lower=_decimal_from_json(
                "probability_lower", raw["probability_lower"]
            ),
            probability_point=_decimal_from_json(
                "probability_point", raw["probability_point"]
            ),
            probability_upper=_decimal_from_json(
                "probability_upper", raw["probability_upper"]
            ),
            net_win_profit_per_stake=_decimal_from_json(
                "net_win_profit_per_stake", raw["net_win_profit_per_stake"]
            ),
            evidence_refs=tuple(refs),
        )

    @property
    def fingerprint_sha256(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class UncertaintySizingPolicy:
    """Owner-selected conservative controls; this is not a probability producer."""

    policy_id: str
    fractional_kelly: Decimal
    max_bankroll_fraction: Decimal
    max_uncertainty_width: Decimal
    min_conservative_ev_per_stake: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        _text("policy_id", self.policy_id, maximum_bytes=160)
        fractional = _decimal("fractional_kelly", self.fractional_kelly)
        max_fraction = _decimal("max_bankroll_fraction", self.max_bankroll_fraction)
        max_width = _decimal("max_uncertainty_width", self.max_uncertainty_width)
        min_edge = _decimal(
            "min_conservative_ev_per_stake", self.min_conservative_ev_per_stake
        )
        if not Decimal("0") < fractional <= Decimal("1"):
            raise UncertaintySizingError("fractional_kelly must be in (0, 1]")
        if not Decimal("0") < max_fraction <= Decimal("1"):
            raise UncertaintySizingError("max_bankroll_fraction must be in (0, 1]")
        if not Decimal("0") <= max_width <= Decimal("1"):
            raise UncertaintySizingError("max_uncertainty_width must be in [0, 1]")
        if min_edge < 0:
            raise UncertaintySizingError(
                "min_conservative_ev_per_stake must be non-negative"
            )

    @property
    def fingerprint_sha256(self) -> str:
        """Bind governed policy identity, method version, and exact parameters."""

        payload = {
            "schema": "autosport.uncertainty_sizing_policy",
            "schema_version": 1,
            "policy_id": self.policy_id,
            "sizing_method_id": _SIZING_METHOD_ID,
            "fractional_kelly": str(self.fractional_kelly),
            "max_bankroll_fraction": str(self.max_bankroll_fraction),
            "max_uncertainty_width": str(self.max_uncertainty_width),
            "min_conservative_ev_per_stake": str(
                self.min_conservative_ev_per_stake
            ),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class UncertaintySizingRequest:
    candidate_id: str
    quote_sha256: str
    decision_ts: str
    bankroll_id: str
    currency: str
    bankroll: Decimal

    def __post_init__(self) -> None:
        _text("candidate_id", self.candidate_id, maximum_bytes=256)
        _sha256("quote_sha256", self.quote_sha256)
        _utc("decision_ts", self.decision_ts)
        _text("bankroll_id", self.bankroll_id, maximum_bytes=160)
        currency = _text("currency", self.currency, maximum_bytes=3)
        if (
            len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
            or currency != currency.upper()
        ):
            raise UncertaintySizingError(
                "currency must be a three-letter uppercase ASCII code"
            )
        bankroll = _decimal("bankroll", self.bankroll)
        if bankroll <= 0:
            raise UncertaintySizingError("bankroll must be strictly positive")


@dataclass(frozen=True, slots=True)
class UncertaintySizingDecision:
    """Sizing prerequisite output. ELIGIBLE never means execution authorized."""

    action: SizingAction
    reasons: tuple[str, ...]
    evidence_fingerprint_sha256: str
    candidate_id: str
    quote_sha256: str
    decision_ts: str
    policy_fingerprint_sha256: str
    bankroll_id: str
    currency: str
    bankroll: Decimal
    conservative_ev_per_stake: Decimal
    conservative_full_kelly_fraction: Decimal
    stake_fraction_ceiling: Decimal
    stake_ceiling: Decimal

    @property
    def decision_fingerprint_sha256(self) -> str:
        """Deterministic result identity including exact sizing-bankroll context."""

        payload = {
            "action": self.action.value,
            "reasons": list(self.reasons),
            "evidence_fingerprint_sha256": self.evidence_fingerprint_sha256,
            "candidate_id": self.candidate_id,
            "quote_sha256": self.quote_sha256,
            "decision_ts": self.decision_ts,
            "policy_fingerprint_sha256": self.policy_fingerprint_sha256,
            "bankroll_id": self.bankroll_id,
            "currency": self.currency,
            "bankroll": str(self.bankroll),
            "conservative_ev_per_stake": str(self.conservative_ev_per_stake),
            "conservative_full_kelly_fraction": str(
                self.conservative_full_kelly_fraction
            ),
            "stake_fraction_ceiling": str(self.stake_fraction_ceiling),
            "stake_ceiling": str(self.stake_ceiling),
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


def evaluate_uncertainty_sizing(
    evidence: UncertaintySizingEvidence,
    request: UncertaintySizingRequest,
    policy: UncertaintySizingPolicy,
) -> UncertaintySizingDecision:
    """Return a conservative stake ceiling or ABSTAIN with deterministic reasons.

    The lower probability interval endpoint is the only probability used in the EV
    and Kelly calculation. Evidence is invalid exactly at ``decision_ts >=
    valid_until``.  Any returned ceiling is an upper bound only; downstream policy
    may reduce it to zero.
    """

    if type(evidence) is not UncertaintySizingEvidence:
        raise TypeError("evidence must be exact UncertaintySizingEvidence")
    if type(request) is not UncertaintySizingRequest:
        raise TypeError("request must be exact UncertaintySizingRequest")
    if type(policy) is not UncertaintySizingPolicy:
        raise TypeError("policy must be exact UncertaintySizingPolicy")

    _, decision = _utc("decision_ts", request.decision_ts)
    _, produced = _utc("produced_at", evidence.produced_at)
    _, valid_until = _utc("valid_until", evidence.valid_until)

    reasons: list[str] = []
    if request.candidate_id != evidence.candidate_id:
        reasons.append("candidate_identity_mismatch")
    if request.quote_sha256 != evidence.quote_sha256:
        reasons.append("quote_identity_mismatch")
    if decision < produced:
        reasons.append("evidence_not_yet_produced")
    if decision >= valid_until:
        reasons.append("evidence_expired")
    if evidence.uncertainty_width > policy.max_uncertainty_width:
        reasons.append("uncertainty_too_wide")

    edge = evidence.conservative_ev_per_stake
    full_kelly = evidence.conservative_full_kelly_fraction
    if edge <= policy.min_conservative_ev_per_stake:
        reasons.append("insufficient_conservative_edge")

    normalized_reasons = tuple(dict.fromkeys(reasons))
    if normalized_reasons:
        fraction = Decimal("0")
        stake = Decimal("0")
        action = SizingAction.ABSTAIN
    else:
        with localcontext(
            Context(prec=_SIZING_ARITHMETIC_PRECISION, rounding=ROUND_FLOOR)
        ):
            fractional_kelly_ceiling = +(full_kelly * policy.fractional_kelly)
        fraction = min(
            fractional_kelly_ceiling,
            policy.max_bankroll_fraction,
        )
        if fraction <= 0:
            # Defensive invariant: a positive conservative edge must imply positive
            # Kelly fraction, but fail closed if arithmetic semantics ever change.
            normalized_reasons = ("non_positive_size",)
            fraction = Decimal("0")
            stake = Decimal("0")
            action = SizingAction.ABSTAIN
        else:
            with localcontext(
                Context(prec=_SIZING_ARITHMETIC_PRECISION, rounding=ROUND_FLOOR)
            ):
                stake = +(request.bankroll * fraction)
            action = SizingAction.ELIGIBLE

    return UncertaintySizingDecision(
        action=action,
        reasons=normalized_reasons,
        evidence_fingerprint_sha256=evidence.fingerprint_sha256,
        candidate_id=request.candidate_id,
        quote_sha256=request.quote_sha256,
        decision_ts=request.decision_ts,
        policy_fingerprint_sha256=policy.fingerprint_sha256,
        bankroll_id=request.bankroll_id,
        currency=request.currency,
        bankroll=request.bankroll,
        conservative_ev_per_stake=edge,
        conservative_full_kelly_fraction=full_kelly,
        stake_fraction_ceiling=fraction,
        stake_ceiling=stake,
    )
