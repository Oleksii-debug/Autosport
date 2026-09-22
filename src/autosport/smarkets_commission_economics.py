"""Smarkets commission economics contract.

This module models the current published Smarkets commission *basis* without
claiming provider-origin or incurred-charge authority. Standard commission is
market-level; Pro and Select commission are per settled matched bet. The API
setup fee is deliberately outside this contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Mapping


_TERMS_VERSION = "4.14"
_TERMS_EFFECTIVE_DATE = "2026-08-14"
_TERMS_URL = "https://help.smarkets.com/hc/en-gb/articles/213469085-Smarkets-Terms-and-Conditions"
_COMMISSION_FAQ_URL = "https://help.smarkets.com/hc/en-gb/articles/212654665-Smarkets-commission-FAQ"
_API_TERMS_URL = "https://help.smarkets.com/hc/en-gb/articles/34697834941085-Smarkets-API-Access-Integration-T-Cs"
_HEX = frozenset("0123456789abcdef")
_MAX_DIGITS = 64
_MIN_EXPONENT = -18
_MAX_EXPONENT = 36
_MAX_ADJUSTED = 36


class SmarketsCommissionEconomicsError(ValueError):
    """Raised when commission evidence is structurally inconsistent."""


class SmarketsCommissionTier(str, Enum):
    STANDARD = "STANDARD"
    PRO = "PRO"
    SELECT = "SELECT"


class SmarketsCommissionBasis(str, Enum):
    MARKET_NET_WINNINGS = "MARKET_NET_WINNINGS"
    PER_SETTLED_MATCHED_BET_ABSOLUTE_PNL = "PER_SETTLED_MATCHED_BET_ABSOLUTE_PNL"


_TIER_RATE: dict[SmarketsCommissionTier, Decimal] = {
    SmarketsCommissionTier.STANDARD: Decimal("0.02"),
    SmarketsCommissionTier.PRO: Decimal("0.01"),
    SmarketsCommissionTier.SELECT: Decimal("0.03"),
}

_TIER_BASIS: dict[SmarketsCommissionTier, SmarketsCommissionBasis] = {
    SmarketsCommissionTier.STANDARD: SmarketsCommissionBasis.MARKET_NET_WINNINGS,
    SmarketsCommissionTier.PRO: SmarketsCommissionBasis.PER_SETTLED_MATCHED_BET_ABSOLUTE_PNL,
    SmarketsCommissionTier.SELECT: SmarketsCommissionBasis.PER_SETTLED_MATCHED_BET_ABSOLUTE_PNL,
}


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise SmarketsCommissionEconomicsError(f"{field} must be canonical non-empty text")
    if "\x00" in value:
        raise SmarketsCommissionEconomicsError(f"{field} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SmarketsCommissionEconomicsError(f"{field} must be valid UTF-8") from exc
    return value


def _sha256_text(value: object, field: str) -> str:
    digest = _text(value, field)
    if len(digest) != 64 or digest != digest.lower() or any(c not in _HEX for c in digest):
        raise SmarketsCommissionEconomicsError(f"{field} must be lowercase SHA-256 hex")
    return digest


def _currency(value: object) -> str:
    code = _text(value, "currency")
    if len(code) != 3 or not code.isascii() or not code.isalpha() or code != code.upper():
        raise SmarketsCommissionEconomicsError("currency must be a 3-letter uppercase ASCII code")
    return code


def _utc(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise SmarketsCommissionEconomicsError(f"{field} must be timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _decimal(value: object, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise SmarketsCommissionEconomicsError(f"{field} must be finite Decimal")
    parts = value.as_tuple()
    exponent = int(parts.exponent)
    if (
        len(parts.digits) > _MAX_DIGITS
        or exponent < _MIN_EXPONENT
        or exponent > _MAX_EXPONENT
        or (value != 0 and value.adjusted() > _MAX_ADJUSTED)
    ):
        raise SmarketsCommissionEconomicsError(f"{field} exceeds bounded Decimal domain")
    return value


def _decimal_text(value: Decimal) -> str:
    value = _decimal(value, "decimal")
    sign, digits, exponent = value.as_tuple()
    coefficient = "".join(str(d) for d in digits) or "0"
    if all(d == 0 for d in digits):
        return "0"
    if exponent >= 0:
        body = coefficient + ("0" * exponent)
    else:
        places = -exponent
        if len(coefficient) > places:
            body = coefficient[:-places] + "." + coefficient[-places:]
        else:
            body = "0." + ("0" * (places - len(coefficient))) + coefficient
        body = body.rstrip("0").rstrip(".")
    return ("-" if sign else "") + body


def _exact_sum(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        return Decimal("0")
    tuples: list[tuple[int, int]] = []
    common_exp: int | None = None
    for value in values:
        _decimal(value, "sum value")
        sign, digits, exponent = value.as_tuple()
        coeff = 0
        for digit in digits:
            coeff = coeff * 10 + digit
        if sign:
            coeff = -coeff
        exponent = int(exponent)
        tuples.append((coeff, exponent))
        common_exp = exponent if common_exp is None else min(common_exp, exponent)
    assert common_exp is not None
    total = sum(coeff * (10 ** (exp - common_exp)) for coeff, exp in tuples)
    sign = 1 if total < 0 else 0
    digits = tuple(int(c) for c in str(abs(total))) if total else (0,)
    return _decimal(Decimal((sign, digits, common_exp)), "sum result")


def _exact_mul(left: Decimal, right: Decimal) -> Decimal:
    _decimal(left, "multiplicand")
    _decimal(right, "multiplier")
    ls, ld, le = left.as_tuple()
    rs, rd, re = right.as_tuple()
    lc = 0
    rc = 0
    for digit in ld:
        lc = lc * 10 + digit
    for digit in rd:
        rc = rc * 10 + digit
    coeff = lc * rc
    sign = int(bool(ls) ^ bool(rs))
    digits = tuple(int(c) for c in str(coeff)) if coeff else (0,)
    return _decimal(Decimal((sign, digits, int(le) + int(re))), "product")


def _abs_exact(value: Decimal) -> Decimal:
    _decimal(value, "pnl")
    sign, digits, exponent = value.as_tuple()
    return Decimal((0, digits, exponent)) if sign else value


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(payload: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SmarketsTierEvidence:
    """Structural evidence that an account tier applies at settlement time.

    This value does not itself prove provider origin. `evidence_sha256` must bind
    the externally acquired account/tier evidence used by a higher-level source
    authority.
    """

    account_id: str
    tier: SmarketsCommissionTier
    effective_from: datetime
    evidence_sha256: str
    effective_until: datetime | None = None

    def __post_init__(self) -> None:
        _text(self.account_id, "account_id")
        if type(self.tier) is not SmarketsCommissionTier:
            raise SmarketsCommissionEconomicsError("tier must be exact SmarketsCommissionTier")
        start = _utc(self.effective_from, "effective_from")
        object.__setattr__(self, "effective_from", start)
        _sha256_text(self.evidence_sha256, "evidence_sha256")
        if self.effective_until is not None:
            end = _utc(self.effective_until, "effective_until")
            if end <= start:
                raise SmarketsCommissionEconomicsError("effective_until must be after effective_from")
            object.__setattr__(self, "effective_until", end)

    def covers(self, settled_at: datetime) -> bool:
        moment = _utc(settled_at, "settled_at")
        return self.effective_from <= moment and (
            self.effective_until is None or moment < self.effective_until
        )


@dataclass(frozen=True, slots=True)
class SmarketsMarketSettlementEconomics:
    """Market-level net P&L input for Standard-tier commission."""

    account_id: str
    market_id: str
    currency: str
    settled_at: datetime
    net_market_pnl: Decimal
    settlement_evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.account_id, "account_id")
        _text(self.market_id, "market_id")
        _currency(self.currency)
        object.__setattr__(self, "settled_at", _utc(self.settled_at, "settled_at"))
        _decimal(self.net_market_pnl, "net_market_pnl")
        _sha256_text(self.settlement_evidence_sha256, "settlement_evidence_sha256")


@dataclass(frozen=True, slots=True)
class SmarketsSettledMatchedBetEconomics:
    """One settled matched bet's realized gross P&L for Pro/Select commission."""

    account_id: str
    market_id: str
    bet_id: str
    currency: str
    settled_at: datetime
    gross_pnl: Decimal
    settlement_evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.account_id, "account_id")
        _text(self.market_id, "market_id")
        _text(self.bet_id, "bet_id")
        _currency(self.currency)
        object.__setattr__(self, "settled_at", _utc(self.settled_at, "settled_at"))
        _decimal(self.gross_pnl, "gross_pnl")
        _sha256_text(self.settlement_evidence_sha256, "settlement_evidence_sha256")


@dataclass(frozen=True, slots=True)
class SmarketsCommissionAssessment:
    account_id: str
    market_id: str
    currency: str
    settled_at: datetime
    tier: SmarketsCommissionTier
    basis: SmarketsCommissionBasis
    rate: Decimal
    modeled_commission: Decimal
    tier_evidence_sha256: str
    settlement_evidence_sha256s: tuple[str, ...]
    assessment_id: str
    terms_version: str = _TERMS_VERSION
    terms_effective_date: str = _TERMS_EFFECTIVE_DATE
    terms_url: str = _TERMS_URL
    commission_faq_url: str = _COMMISSION_FAQ_URL
    api_setup_fee_included: bool = False
    provider_incurred_exact: bool = False
    execution_authorized: bool = False

    def __post_init__(self) -> None:
        _text(self.account_id, "account_id")
        _text(self.market_id, "market_id")
        _currency(self.currency)
        object.__setattr__(self, "settled_at", _utc(self.settled_at, "settled_at"))
        if type(self.tier) is not SmarketsCommissionTier:
            raise SmarketsCommissionEconomicsError("tier must be exact SmarketsCommissionTier")
        if type(self.basis) is not SmarketsCommissionBasis:
            raise SmarketsCommissionEconomicsError("basis must be exact SmarketsCommissionBasis")
        _decimal(self.rate, "rate")
        _decimal(self.modeled_commission, "modeled_commission")
        _sha256_text(self.tier_evidence_sha256, "tier_evidence_sha256")
        if type(self.settlement_evidence_sha256s) is not tuple or not self.settlement_evidence_sha256s:
            raise SmarketsCommissionEconomicsError("settlement evidence digests must be a non-empty tuple")
        for digest in self.settlement_evidence_sha256s:
            _sha256_text(digest, "settlement_evidence_sha256")
        if tuple(sorted(set(self.settlement_evidence_sha256s))) != self.settlement_evidence_sha256s:
            raise SmarketsCommissionEconomicsError("settlement evidence digests must be unique and sorted")
        _sha256_text(self.assessment_id, "assessment_id")
        if self.terms_version != _TERMS_VERSION or self.terms_effective_date != _TERMS_EFFECTIVE_DATE:
            raise SmarketsCommissionEconomicsError("assessment must bind the implemented terms revision")
        if self.terms_url != _TERMS_URL or self.commission_faq_url != _COMMISSION_FAQ_URL:
            raise SmarketsCommissionEconomicsError("assessment must bind canonical Smarkets terms sources")
        if self.api_setup_fee_included is not False:
            raise SmarketsCommissionEconomicsError("API setup fee must remain outside wager commission")
        if self.provider_incurred_exact is not False or self.execution_authorized is not False:
            raise SmarketsCommissionEconomicsError("modeled commission cannot mint provider/execution authority")
        if self.rate != _TIER_RATE[self.tier] or self.basis is not _TIER_BASIS[self.tier]:
            raise SmarketsCommissionEconomicsError("tier rate/basis mismatch")
        if self.modeled_commission < 0:
            raise SmarketsCommissionEconomicsError("modeled commission must be non-negative")


class SmarketsCommissionEconomics:
    """Terms-bound calculator that preserves provider commission granularity."""

    terms_version = _TERMS_VERSION
    terms_effective_date = _TERMS_EFFECTIVE_DATE
    terms_url = _TERMS_URL
    commission_faq_url = _COMMISSION_FAQ_URL
    api_terms_url = _API_TERMS_URL

    @staticmethod
    def assess_standard(
        *,
        tier_evidence: SmarketsTierEvidence,
        settlement: SmarketsMarketSettlementEconomics,
    ) -> SmarketsCommissionAssessment:
        if type(tier_evidence) is not SmarketsTierEvidence or type(settlement) is not SmarketsMarketSettlementEconomics:
            raise SmarketsCommissionEconomicsError("standard assessment requires exact canonical evidence types")
        if tier_evidence.tier is not SmarketsCommissionTier.STANDARD:
            raise SmarketsCommissionEconomicsError("market-net basis is only valid for Standard tier")
        SmarketsCommissionEconomics._bind_common(tier_evidence, settlement)
        base = settlement.net_market_pnl if settlement.net_market_pnl > 0 else Decimal("0")
        charge = _exact_mul(base, _TIER_RATE[SmarketsCommissionTier.STANDARD])
        digests = (settlement.settlement_evidence_sha256,)
        return SmarketsCommissionEconomics._assessment(
            tier_evidence=tier_evidence,
            account_id=settlement.account_id,
            market_id=settlement.market_id,
            currency=settlement.currency,
            settled_at=settlement.settled_at,
            settlement_digests=digests,
            modeled_commission=charge,
        )

    @staticmethod
    def assess_per_bet(
        *,
        tier_evidence: SmarketsTierEvidence,
        bets: tuple[SmarketsSettledMatchedBetEconomics, ...],
    ) -> SmarketsCommissionAssessment:
        if type(tier_evidence) is not SmarketsTierEvidence:
            raise SmarketsCommissionEconomicsError("tier_evidence must be exact SmarketsTierEvidence")
        if tier_evidence.tier not in {SmarketsCommissionTier.PRO, SmarketsCommissionTier.SELECT}:
            raise SmarketsCommissionEconomicsError("per-bet basis is only valid for Pro or Select tier")
        if type(bets) is not tuple or not bets:
            raise SmarketsCommissionEconomicsError("bets must be a non-empty tuple")
        if any(type(row) is not SmarketsSettledMatchedBetEconomics for row in bets):
            raise SmarketsCommissionEconomicsError("bets must contain exact canonical rows")
        first = bets[0]
        seen: set[str] = set()
        terms: list[Decimal] = []
        digests: list[str] = []
        for row in bets:
            if row.bet_id in seen:
                raise SmarketsCommissionEconomicsError("duplicate bet_id in settlement population")
            seen.add(row.bet_id)
            if (
                row.account_id != first.account_id
                or row.market_id != first.market_id
                or row.currency != first.currency
                or row.settled_at != first.settled_at
            ):
                raise SmarketsCommissionEconomicsError("per-bet rows must share exact account/market/currency/settlement")
            SmarketsCommissionEconomics._bind_common(tier_evidence, row)
            terms.append(_exact_mul(_abs_exact(row.gross_pnl), _TIER_RATE[tier_evidence.tier]))
            digests.append(row.settlement_evidence_sha256)
        digest_tuple = tuple(sorted(set(digests)))
        if len(digest_tuple) != len(digests):
            raise SmarketsCommissionEconomicsError("settlement evidence digest reuse is ambiguous")
        return SmarketsCommissionEconomics._assessment(
            tier_evidence=tier_evidence,
            account_id=first.account_id,
            market_id=first.market_id,
            currency=first.currency,
            settled_at=first.settled_at,
            settlement_digests=digest_tuple,
            modeled_commission=_exact_sum(tuple(terms)),
        )

    @staticmethod
    def _bind_common(tier_evidence: SmarketsTierEvidence, settlement: object) -> None:
        account_id = getattr(settlement, "account_id")
        settled_at = getattr(settlement, "settled_at")
        if tier_evidence.account_id != account_id:
            raise SmarketsCommissionEconomicsError("tier evidence account does not match settlement account")
        if not tier_evidence.covers(settled_at):
            raise SmarketsCommissionEconomicsError("tier evidence does not cover settlement time")

    @staticmethod
    def _assessment(
        *,
        tier_evidence: SmarketsTierEvidence,
        account_id: str,
        market_id: str,
        currency: str,
        settled_at: datetime,
        settlement_digests: tuple[str, ...],
        modeled_commission: Decimal,
    ) -> SmarketsCommissionAssessment:
        tier = tier_evidence.tier
        payload = {
            "schema": "autosport.smarkets_commission_assessment",
            "schema_version": 1,
            "account_id": account_id,
            "market_id": market_id,
            "currency": currency,
            "settled_at": _utc(settled_at, "settled_at").isoformat(),
            "tier": tier.value,
            "basis": _TIER_BASIS[tier].value,
            "rate": _decimal_text(_TIER_RATE[tier]),
            "modeled_commission": _decimal_text(modeled_commission),
            "tier_evidence_sha256": tier_evidence.evidence_sha256,
            "settlement_evidence_sha256s": list(settlement_digests),
            "terms_version": _TERMS_VERSION,
            "terms_effective_date": _TERMS_EFFECTIVE_DATE,
            "terms_url": _TERMS_URL,
            "commission_faq_url": _COMMISSION_FAQ_URL,
            "api_setup_fee_included": False,
            "provider_incurred_exact": False,
            "execution_authorized": False,
        }
        return SmarketsCommissionAssessment(
            account_id=account_id,
            market_id=market_id,
            currency=currency,
            settled_at=settled_at,
            tier=tier,
            basis=_TIER_BASIS[tier],
            rate=_TIER_RATE[tier],
            modeled_commission=modeled_commission,
            tier_evidence_sha256=tier_evidence.evidence_sha256,
            settlement_evidence_sha256s=settlement_digests,
            assessment_id=_digest(payload),
        )
