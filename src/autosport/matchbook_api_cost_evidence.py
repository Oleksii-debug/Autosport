from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
import re
from typing import Any


MATCHBOOK_PRICING_URL = "https://developers.matchbook.com/docs/pricing"
MATCHBOOK_FAIR_USAGE_URL = "https://developers.matchbook.com/docs/fair-usage-policy"
MATCHBOOK_GET_BLOCK_SIZE = 1_000_000
MATCHBOOK_GET_BLOCK_PRICE_GBP = Decimal("100")
MATCHBOOK_WRITE_QUALIFICATION = "NON_CHARGEABLE_ONLY_AT_REASONABLE_FREQUENCY"
_SCHEMA = "autosport.matchbook_api_cost_evidence.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = object()


class MatchbookApiCostEvidenceError(ValueError):
    """Raised when Matchbook API usage/cost evidence is not fail-closed."""


class BillingCalendarBasis(StrEnum):
    CONFIGURED_ASSUMPTION = "CONFIGURED_ASSUMPTION"


class PolicyAmountTruth(StrEnum):
    CONFIGURED_CALENDAR_ESTIMATE_GBP = "CONFIGURED_CALENDAR_ESTIMATE_GBP"
    REMAINDER_UNRESOLVED = "REMAINDER_UNRESOLVED"


class FxTruth(StrEnum):
    NOT_NEEDED_GBP = "NOT_NEEDED_GBP"
    UNRESOLVED_PROVIDER_FX = "UNRESOLVED_PROVIDER_FX"


class CashTruth(StrEnum):
    UNRECONCILED = "UNRECONCILED"


class MeterContinuityTruth(StrEnum):
    PROCESS_LOCAL_ONLY = "PROCESS_LOCAL_ONLY"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise MatchbookApiCostEvidenceError(f"{name} must be canonical non-empty text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise MatchbookApiCostEvidenceError(f"{name} must be lowercase SHA-256")
    return text


def _aware(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise MatchbookApiCostEvidenceError(f"{name} must be timezone-aware datetime")
    return value


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise MatchbookApiCostEvidenceError(f"{name} must be finite Decimal")
    return value


def _dt(value: datetime) -> str:
    return _aware(value, "datetime").isoformat(timespec="microseconds")


def _money(value: Decimal) -> str:
    return format(_decimal(value, "money"), "f")


def _digest(payload: Any) -> str:
    try:
        raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise MatchbookApiCostEvidenceError("evidence payload is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class MatchbookBillingPeriod:
    period_id: str
    starts_at: datetime
    ends_at: datetime
    timezone_name: str
    config_sha256: str
    basis: BillingCalendarBasis = field(default=BillingCalendarBasis.CONFIGURED_ASSUMPTION, init=False)
    _token: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise MatchbookApiCostEvidenceError("billing period must be issued by configured_billing_period")
        _text(self.period_id, "period_id")
        start, end = _aware(self.starts_at, "starts_at"), _aware(self.ends_at, "ends_at")
        if start >= end:
            raise MatchbookApiCostEvidenceError("billing period starts_at must be before ends_at")
        _text(self.timezone_name, "timezone_name")
        _sha(self.config_sha256, "config_sha256")

    @property
    def evidence_sha256(self) -> str:
        return _digest({"schema": _SCHEMA, "kind": "billing_period", "period_id": self.period_id,
                        "starts_at": _dt(self.starts_at), "ends_at": _dt(self.ends_at),
                        "timezone_name": self.timezone_name, "config_sha256": self.config_sha256,
                        "basis": self.basis.value})


def configured_billing_period(*, period_id: str, starts_at: datetime, ends_at: datetime,
                              timezone_name: str, config_sha256: str) -> MatchbookBillingPeriod:
    """Create an explicit assumption; public Matchbook docs do not establish billing timezone here."""
    return MatchbookBillingPeriod(period_id, starts_at, ends_at, timezone_name, config_sha256, _token=_TOKEN)


@dataclass(frozen=True, slots=True)
class MatchbookPricingPolicySnapshot:
    observed_at: datetime
    source_sha256: str
    source_url: str
    get_block_size: int
    get_block_price_gbp: Decimal
    write_qualification: str
    fair_usage_url: str
    _token: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise MatchbookApiCostEvidenceError("pricing policy must be issued by public_matchbook_pricing_policy")
        _aware(self.observed_at, "observed_at"); _sha(self.source_sha256, "source_sha256")
        if self.source_url != MATCHBOOK_PRICING_URL or self.fair_usage_url != MATCHBOOK_FAIR_USAGE_URL:
            raise MatchbookApiCostEvidenceError("pricing policy source URL drift")
        if self.get_block_size != MATCHBOOK_GET_BLOCK_SIZE or self.get_block_price_gbp != MATCHBOOK_GET_BLOCK_PRICE_GBP:
            raise MatchbookApiCostEvidenceError("Matchbook GET pricing drifted from frozen public policy")
        if self.write_qualification != MATCHBOOK_WRITE_QUALIFICATION:
            raise MatchbookApiCostEvidenceError("Matchbook WRITE qualification drifted")

    @property
    def policy_sha256(self) -> str:
        return _digest({"schema": _SCHEMA, "kind": "pricing_policy", "observed_at": _dt(self.observed_at),
                        "source_sha256": self.source_sha256, "source_url": self.source_url,
                        "get_block_size": self.get_block_size, "get_block_price_gbp": _money(self.get_block_price_gbp),
                        "write_qualification": self.write_qualification, "fair_usage_url": self.fair_usage_url})


def public_matchbook_pricing_policy(*, observed_at: datetime, source_sha256: str) -> MatchbookPricingPolicySnapshot:
    return MatchbookPricingPolicySnapshot(observed_at, source_sha256, MATCHBOOK_PRICING_URL,
        MATCHBOOK_GET_BLOCK_SIZE, MATCHBOOK_GET_BLOCK_PRICE_GBP, MATCHBOOK_WRITE_QUALIFICATION,
        MATCHBOOK_FAIR_USAGE_URL, _token=_TOKEN)


@dataclass(frozen=True, slots=True)
class MatchbookPolicyMath:
    request_count: int
    completed_blocks: int
    remainder_requests: int
    completed_block_gbp: Decimal
    exact_policy_gbp: Decimal | None


def calculate_policy_math(request_count: int, policy: MatchbookPricingPolicySnapshot) -> MatchbookPolicyMath:
    """Apply only the unambiguous whole-million part of the published GET rule."""
    if type(policy) is not MatchbookPricingPolicySnapshot:
        raise MatchbookApiCostEvidenceError("policy must be exact MatchbookPricingPolicySnapshot")
    if type(request_count) is not int or isinstance(request_count, bool) or request_count < 0:
        raise MatchbookApiCostEvidenceError("request_count must be non-negative integer")
    blocks, remainder = divmod(request_count, policy.get_block_size)
    completed = policy.get_block_price_gbp * blocks
    return MatchbookPolicyMath(request_count, blocks, remainder, completed, completed if remainder == 0 else None)


@dataclass(frozen=True, slots=True)
class MatchbookApiUsageSnapshot:
    application_context_sha256: str
    billing_period_sha256: str
    pricing_policy_sha256: str
    request_count: int
    first_sequence: int | None
    last_sequence: int | None
    first_emitted_at: datetime | None
    last_emitted_at: datetime | None
    rolling_emission_sha256: str
    observed_at: datetime
    continuity_truth: MeterContinuityTruth
    evidence_sha256: str
    _token: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise MatchbookApiCostEvidenceError("usage snapshot must be issued by MatchbookApiRequestMeter")
        for name in ("application_context_sha256", "billing_period_sha256", "pricing_policy_sha256", "rolling_emission_sha256"):
            _sha(getattr(self, name), name)
        observed = _aware(self.observed_at, "observed_at")
        if self.continuity_truth is not MeterContinuityTruth.PROCESS_LOCAL_ONLY:
            raise MatchbookApiCostEvidenceError("this seam cannot mint durable request-meter continuity")
        if type(self.request_count) is not int or self.request_count < 0:
            raise MatchbookApiCostEvidenceError("request_count must be non-negative integer")
        if self.request_count == 0:
            if any(v is not None for v in (self.first_sequence, self.last_sequence, self.first_emitted_at, self.last_emitted_at)):
                raise MatchbookApiCostEvidenceError("empty usage cannot carry emission bounds")
        else:
            if type(self.first_sequence) is not int or type(self.last_sequence) is not int or self.first_sequence <= 0:
                raise MatchbookApiCostEvidenceError("usage sequence bounds are invalid")
            if self.last_sequence - self.first_sequence + 1 != self.request_count:
                raise MatchbookApiCostEvidenceError("usage sequence bounds do not match request_count")
            first, last = _aware(self.first_emitted_at, "first_emitted_at"), _aware(self.last_emitted_at, "last_emitted_at")
            if first > last or last > observed:
                raise MatchbookApiCostEvidenceError("usage emission/observation chronology is invalid")
        _sha(self.evidence_sha256, "evidence_sha256")
        if self.evidence_sha256 != _usage_digest(self):
            raise MatchbookApiCostEvidenceError("usage evidence digest mismatch")


def _usage_payload(v: MatchbookApiUsageSnapshot | dict[str, Any]) -> dict[str, Any]:
    g = v.get if isinstance(v, dict) else lambda k: getattr(v, k)
    return {"schema": _SCHEMA, "kind": "usage_snapshot",
            "application_context_sha256": g("application_context_sha256"), "billing_period_sha256": g("billing_period_sha256"),
            "pricing_policy_sha256": g("pricing_policy_sha256"), "request_count": g("request_count"),
            "first_sequence": g("first_sequence"), "last_sequence": g("last_sequence"),
            "first_emitted_at": None if g("first_emitted_at") is None else _dt(g("first_emitted_at")),
            "last_emitted_at": None if g("last_emitted_at") is None else _dt(g("last_emitted_at")),
            "rolling_emission_sha256": g("rolling_emission_sha256"), "observed_at": _dt(g("observed_at")),
            "continuity_truth": g("continuity_truth").value}


def _usage_digest(v: MatchbookApiUsageSnapshot | dict[str, Any]) -> str:
    return _digest(_usage_payload(v))


class MatchbookApiRequestMeter:
    """O(1) process-local physical-GET meter; durable restart continuity is deliberately not claimed."""
    __slots__ = ("_app", "_period", "_policy", "_count", "_first_seq", "_last_seq", "_first_at", "_last_at", "_rolling")

    def __init__(self, *, application_context_sha256: str, billing_period: MatchbookBillingPeriod,
                 pricing_policy: MatchbookPricingPolicySnapshot) -> None:
        self._app = _sha(application_context_sha256, "application_context_sha256")
        if type(billing_period) is not MatchbookBillingPeriod or type(pricing_policy) is not MatchbookPricingPolicySnapshot:
            raise MatchbookApiCostEvidenceError("meter requires exact billing-period and pricing-policy objects")
        if pricing_policy.observed_at > billing_period.starts_at:
            raise MatchbookApiCostEvidenceError("pricing policy must be captured no later than billing-period start")
        self._period, self._policy = billing_period, pricing_policy
        self._count = 0; self._first_seq = None; self._last_seq = None; self._first_at = None; self._last_at = None
        self._rolling = _digest({"schema": _SCHEMA, "kind": "emission_genesis", "application_context_sha256": self._app,
                                 "billing_period_sha256": billing_period.evidence_sha256, "pricing_policy_sha256": pricing_policy.policy_sha256})

    @property
    def request_count(self) -> int: return self._count

    @property
    def rolling_emission_sha256(self) -> str: return self._rolling

    def record_get_emitted(self, *, sequence: int, request_fingerprint_sha256: str, emitted_at: datetime) -> None:
        if type(sequence) is not int or isinstance(sequence, bool) or sequence <= 0:
            raise MatchbookApiCostEvidenceError("sequence must be positive integer")
        expected = 1 if self._last_seq is None else self._last_seq + 1
        if sequence != expected:
            raise MatchbookApiCostEvidenceError(f"physical GET sequence must advance exactly once; expected {expected}")
        fingerprint, emitted = _sha(request_fingerprint_sha256, "request_fingerprint_sha256"), _aware(emitted_at, "emitted_at")
        if not (self._period.starts_at <= emitted < self._period.ends_at):
            raise MatchbookApiCostEvidenceError("GET emission is outside bound billing period")
        if self._last_at is not None and emitted < self._last_at:
            raise MatchbookApiCostEvidenceError("GET emission time cannot move backwards")
        next_digest = _digest({"schema": _SCHEMA, "kind": "get_emission", "previous_sha256": self._rolling,
                               "sequence": sequence, "request_fingerprint_sha256": fingerprint, "emitted_at": _dt(emitted)})
        if self._first_seq is None: self._first_seq, self._first_at = sequence, emitted
        self._last_seq, self._last_at, self._count, self._rolling = sequence, emitted, self._count + 1, next_digest

    def snapshot(self, *, observed_at: datetime) -> MatchbookApiUsageSnapshot:
        observed = _aware(observed_at, "observed_at")
        if self._last_at is not None and observed < self._last_at:
            raise MatchbookApiCostEvidenceError("usage snapshot cannot predate last emission")
        data = dict(application_context_sha256=self._app, billing_period_sha256=self._period.evidence_sha256,
                    pricing_policy_sha256=self._policy.policy_sha256, request_count=self._count,
                    first_sequence=self._first_seq, last_sequence=self._last_seq, first_emitted_at=self._first_at,
                    last_emitted_at=self._last_at, rolling_emission_sha256=self._rolling, observed_at=observed,
                    continuity_truth=MeterContinuityTruth.PROCESS_LOCAL_ONLY)
        return MatchbookApiUsageSnapshot(**data, evidence_sha256=_usage_digest(data), _token=_TOKEN)


@dataclass(frozen=True, slots=True)
class MatchbookApiCostAccrual:
    usage_evidence_sha256: str
    billing_period_sha256: str
    pricing_policy_sha256: str
    request_count: int
    completed_blocks: int
    remainder_requests: int
    completed_block_gbp: Decimal
    policy_gbp_amount: Decimal | None
    amount_truth: PolicyAmountTruth
    account_currency: str
    fx_truth: FxTruth
    account_currency_amount: Decimal | None
    cash_truth: CashTruth
    allocation_state: str
    meter_continuity_truth: MeterContinuityTruth
    evidence_sha256: str
    _token: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise MatchbookApiCostEvidenceError("cost accrual must be issued by derive_matchbook_api_cost_accrual")
        for name in ("usage_evidence_sha256", "billing_period_sha256", "pricing_policy_sha256", "evidence_sha256"):
            _sha(getattr(self, name), name)
        _decimal(self.completed_block_gbp, "completed_block_gbp")
        if self.policy_gbp_amount is not None: _decimal(self.policy_gbp_amount, "policy_gbp_amount")
        currency = _text(self.account_currency, "account_currency")
        if len(currency) != 3 or not currency.isascii() or currency != currency.upper():
            raise MatchbookApiCostEvidenceError("account_currency must be three uppercase ASCII letters")
        if self.remainder_requests:
            if self.policy_gbp_amount is not None or self.amount_truth is not PolicyAmountTruth.REMAINDER_UNRESOLVED:
                raise MatchbookApiCostEvidenceError("remainder usage cannot mint prorated policy total")
        elif self.policy_gbp_amount != self.completed_block_gbp or self.amount_truth is not PolicyAmountTruth.CONFIGURED_CALENDAR_ESTIMATE_GBP:
            raise MatchbookApiCostEvidenceError("whole-block configured-calendar estimate is inconsistent")
        if currency == "GBP":
            if self.fx_truth is not FxTruth.NOT_NEEDED_GBP or self.account_currency_amount != self.policy_gbp_amount:
                raise MatchbookApiCostEvidenceError("GBP account amount/fx state is inconsistent")
        elif self.fx_truth is not FxTruth.UNRESOLVED_PROVIDER_FX or self.account_currency_amount is not None:
            raise MatchbookApiCostEvidenceError("non-GBP provider FX remains unresolved")
        if self.cash_truth is not CashTruth.UNRECONCILED or self.allocation_state != "UNALLOCATED_SHARED_PROVIDER_COST":
            raise MatchbookApiCostEvidenceError("this seam cannot mint cash truth or campaign allocation")
        if self.meter_continuity_truth is not MeterContinuityTruth.PROCESS_LOCAL_ONLY:
            raise MatchbookApiCostEvidenceError("this seam cannot mint durable request-meter continuity")
        if self.evidence_sha256 != _accrual_digest(self):
            raise MatchbookApiCostEvidenceError("cost accrual digest mismatch")


def _accrual_payload(v: MatchbookApiCostAccrual | dict[str, Any]) -> dict[str, Any]:
    g = v.get if isinstance(v, dict) else lambda k: getattr(v, k)
    return {"schema": _SCHEMA, "kind": "api_cost_accrual", "usage_evidence_sha256": g("usage_evidence_sha256"),
            "billing_period_sha256": g("billing_period_sha256"), "pricing_policy_sha256": g("pricing_policy_sha256"),
            "request_count": g("request_count"), "completed_blocks": g("completed_blocks"), "remainder_requests": g("remainder_requests"),
            "completed_block_gbp": _money(g("completed_block_gbp")),
            "policy_gbp_amount": None if g("policy_gbp_amount") is None else _money(g("policy_gbp_amount")),
            "amount_truth": g("amount_truth").value, "account_currency": g("account_currency"), "fx_truth": g("fx_truth").value,
            "account_currency_amount": None if g("account_currency_amount") is None else _money(g("account_currency_amount")),
            "cash_truth": g("cash_truth").value, "allocation_state": g("allocation_state"),
            "meter_continuity_truth": g("meter_continuity_truth").value}


def _accrual_digest(v: MatchbookApiCostAccrual | dict[str, Any]) -> str:
    return _digest(_accrual_payload(v))


def derive_matchbook_api_cost_accrual(*, usage: MatchbookApiUsageSnapshot, billing_period: MatchbookBillingPeriod,
                                      pricing_policy: MatchbookPricingPolicySnapshot, account_currency: str) -> MatchbookApiCostAccrual:
    if type(usage) is not MatchbookApiUsageSnapshot or type(billing_period) is not MatchbookBillingPeriod or type(pricing_policy) is not MatchbookPricingPolicySnapshot:
        raise MatchbookApiCostEvidenceError("accrual requires exact Autosport evidence objects")
    if usage.billing_period_sha256 != billing_period.evidence_sha256:
        raise MatchbookApiCostEvidenceError("usage belongs to a different billing period")
    if usage.pricing_policy_sha256 != pricing_policy.policy_sha256:
        raise MatchbookApiCostEvidenceError("usage belongs to a different pricing policy")
    math = calculate_policy_math(usage.request_count, pricing_policy)
    policy_amount = None if math.remainder_requests else math.exact_policy_gbp
    amount_truth = PolicyAmountTruth.REMAINDER_UNRESOLVED if math.remainder_requests else PolicyAmountTruth.CONFIGURED_CALENDAR_ESTIMATE_GBP
    currency = _text(account_currency, "account_currency")
    if len(currency) != 3 or not currency.isascii() or currency != currency.upper():
        raise MatchbookApiCostEvidenceError("account_currency must be three uppercase ASCII letters")
    fx_truth, account_amount = (FxTruth.NOT_NEEDED_GBP, policy_amount) if currency == "GBP" else (FxTruth.UNRESOLVED_PROVIDER_FX, None)
    data = dict(usage_evidence_sha256=usage.evidence_sha256, billing_period_sha256=billing_period.evidence_sha256,
                pricing_policy_sha256=pricing_policy.policy_sha256, request_count=usage.request_count,
                completed_blocks=math.completed_blocks, remainder_requests=math.remainder_requests,
                completed_block_gbp=math.completed_block_gbp, policy_gbp_amount=policy_amount, amount_truth=amount_truth,
                account_currency=currency, fx_truth=fx_truth, account_currency_amount=account_amount,
                cash_truth=CashTruth.UNRECONCILED, allocation_state="UNALLOCATED_SHARED_PROVIDER_COST",
                meter_continuity_truth=usage.continuity_truth)
    return MatchbookApiCostAccrual(**data, evidence_sha256=_accrual_digest(data), _token=_TOKEN)


__all__ = ["BillingCalendarBasis", "CashTruth", "FxTruth", "MATCHBOOK_FAIR_USAGE_URL", "MATCHBOOK_GET_BLOCK_PRICE_GBP",
           "MATCHBOOK_GET_BLOCK_SIZE", "MATCHBOOK_PRICING_URL", "MATCHBOOK_WRITE_QUALIFICATION", "MatchbookApiCostAccrual",
           "MatchbookApiCostEvidenceError", "MatchbookApiRequestMeter", "MatchbookApiUsageSnapshot", "MatchbookBillingPeriod",
           "MatchbookPolicyMath", "MatchbookPricingPolicySnapshot", "MeterContinuityTruth", "PolicyAmountTruth",
           "calculate_policy_math", "configured_billing_period", "derive_matchbook_api_cost_accrual", "public_matchbook_pricing_policy"]
