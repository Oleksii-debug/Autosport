"""Fail-closed Smarkets entitlement and commercial-terms evidence.

No network access, credentials, order submission, or execution authorization lives
here. HTTP reachability never establishes entitlement. Positive decisions require
product-issued approval evidence bound to purpose, provider scope, freshness and a
recorded rate policy. Setup cost is commercial evidence only, never per-bet or
incurred-cost truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from weakref import ref

SCHEMA_VERSION = 1
VENUE_ID = "smarkets"
_ENT_FAMILY = "smarkets.provider-entitlement.v1"
_COST_FAMILY = "smarkets.provider-setup-cost.v1"


class SmarketsEntitlementError(ValueError):
    pass


class SmarketsApprovalState(str, Enum):
    APPROVED = "approved"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class SmarketsPurpose(str, Enum):
    MARKET_DATA_FOR_TRADING = "market_data_for_trading"
    ACCOUNT_RECONCILIATION = "account_reconciliation"
    ORDER_EXECUTION = "order_execution"
    RESEARCH = "research"
    BENCHMARKING = "benchmarking"
    REDISTRIBUTION = "redistribution"


_PROVIDER_PROHIBITED_PURPOSES = frozenset({SmarketsPurpose.BENCHMARKING})


def _text(v: object, name: str) -> str:
    if type(v) is not str or not v or v != v.strip() or "\x00" in v:
        raise SmarketsEntitlementError(f"{name} must be canonical non-empty text")
    try:
        v.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SmarketsEntitlementError(f"{name} must be UTF-8") from exc
    return v


def _sha(v: object, name: str) -> str:
    s = _text(v, name)
    if len(s) != 64 or any(c not in "0123456789abcdef" for c in s):
        raise SmarketsEntitlementError(f"{name} must be lowercase SHA-256 hex")
    return s


def _dt_text(v: object, name: str) -> datetime:
    s = _text(v, name)
    if not s.endswith("Z"):
        raise SmarketsEntitlementError(f"{name} must use canonical UTC Z notation")
    try:
        d = datetime.fromisoformat(s[:-1] + "+00:00")
    except ValueError as exc:
        raise SmarketsEntitlementError(f"{name} must be ISO-8601") from exc
    canonical = d.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if d.utcoffset() != timezone.utc.utcoffset(d) or s != canonical:
        raise SmarketsEntitlementError(f"{name} must use canonical microsecond UTC encoding")
    return d


def _utc(v: object, name: str) -> datetime:
    if type(v) is not datetime or v.tzinfo is None or v.utcoffset() != timezone.utc.utcoffset(v):
        raise SmarketsEntitlementError(f"{name} must be UTC datetime")
    return v


def _dt(v: datetime) -> str:
    return _utc(v, "datetime").isoformat(timespec="microseconds").replace("+00:00", "Z")


def _posint(v: object, name: str) -> int:
    if type(v) is not int or v <= 0:
        raise SmarketsEntitlementError(f"{name} must be a positive integer")
    return v


def _money(v: object, name: str) -> Decimal:
    if type(v) is not Decimal or not v.is_finite() or v <= 0:
        raise SmarketsEntitlementError(f"{name} must be a finite positive Decimal")
    return v


def _currency(v: object) -> str:
    s = _text(v, "currency")
    if not s.isascii() or s != s.upper() or not s.isalnum() or not 3 <= len(s) <= 8:
        raise SmarketsEntitlementError("currency must be uppercase ASCII alphanumeric provider currency")
    return s


def _digest(v: object) -> str:
    try:
        raw = json.dumps(v, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SmarketsEntitlementError("evidence payload is not canonical JSON") from exc
    return sha256(raw).hexdigest()


def _texts(v: object, name: str) -> tuple[str, ...]:
    if type(v) is not tuple:
        raise SmarketsEntitlementError(f"{name} must be a tuple")
    out = tuple(_text(x, name) for x in v)
    if out != tuple(sorted(out)) or len(out) != len(set(out)):
        raise SmarketsEntitlementError(f"{name} must be sorted and unique")
    return out


def _purposes(v: object) -> tuple[SmarketsPurpose, ...]:
    if type(v) is not tuple or not v or any(type(x) is not SmarketsPurpose for x in v):
        raise SmarketsEntitlementError("approved_purposes must contain exact SmarketsPurpose values")
    if v != tuple(sorted(v, key=lambda x: x.value)) or len(v) != len(set(v)):
        raise SmarketsEntitlementError("approved_purposes must be sorted and unique")
    return v


@dataclass(frozen=True, slots=True, weakref_slot=True)
class SmarketsEntitlementObservation:
    account_scope: str
    approval_ref: str
    approval_state: SmarketsApprovalState
    approved_purposes: tuple[SmarketsPurpose, ...]
    all_events_approved: bool
    allowed_event_ids: tuple[str, ...]
    all_markets_approved: bool
    allowed_market_ids: tuple[str, ...]
    rate_policy_ref: str
    max_requests_per_window: int
    rate_window_seconds: int
    observed_at: str
    source_document_sha256: str
    evidence_sha256: str
    venue_id: str = VENUE_ID
    source_family: str = _ENT_FAMILY
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.venue_id != VENUE_ID or self.source_family != _ENT_FAMILY or self.schema_version != SCHEMA_VERSION:
            raise SmarketsEntitlementError("entitlement schema/source mismatch")
        _text(self.account_scope, "account_scope")
        _text(self.approval_ref, "approval_ref")
        if type(self.approval_state) is not SmarketsApprovalState:
            raise SmarketsEntitlementError("approval_state must be exact SmarketsApprovalState")
        _purposes(self.approved_purposes)
        if type(self.all_events_approved) is not bool or type(self.all_markets_approved) is not bool:
            raise SmarketsEntitlementError("all-scope flags must be bool")
        events = _texts(self.allowed_event_ids, "allowed_event_ids")
        markets = _texts(self.allowed_market_ids, "allowed_market_ids")
        if self.all_events_approved and events:
            raise SmarketsEntitlementError("all-events approval cannot also carry explicit event IDs")
        if not self.all_events_approved and not events:
            raise SmarketsEntitlementError("bounded entitlement requires explicit event scope")
        if self.all_markets_approved and markets:
            raise SmarketsEntitlementError("all-markets approval cannot also carry explicit market IDs")
        if not self.all_markets_approved and not markets:
            raise SmarketsEntitlementError("bounded entitlement requires explicit market scope")
        _text(self.rate_policy_ref, "rate_policy_ref")
        _posint(self.max_requests_per_window, "max_requests_per_window")
        _posint(self.rate_window_seconds, "rate_window_seconds")
        _dt_text(self.observed_at, "observed_at")
        _sha(self.source_document_sha256, "source_document_sha256")
        _sha(self.evidence_sha256, "evidence_sha256")
        if self.evidence_sha256 != _digest(self.payload()):
            raise SmarketsEntitlementError("entitlement evidence digest mismatch")

    def payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.smarkets_entitlement_observation", "schema_version": self.schema_version,
            "venue_id": self.venue_id, "source_family": self.source_family, "account_scope": self.account_scope,
            "approval_ref": self.approval_ref, "approval_state": self.approval_state.value,
            "approved_purposes": [x.value for x in self.approved_purposes],
            "all_events_approved": self.all_events_approved, "allowed_event_ids": list(self.allowed_event_ids),
            "all_markets_approved": self.all_markets_approved, "allowed_market_ids": list(self.allowed_market_ids),
            "rate_policy_ref": self.rate_policy_ref, "max_requests_per_window": self.max_requests_per_window,
            "rate_window_seconds": self.rate_window_seconds, "observed_at": self.observed_at,
            "source_document_sha256": self.source_document_sha256,
        }


@dataclass(frozen=True, slots=True)
class SmarketsEntitlementDecision:
    entitled: bool
    reason: str
    purpose: SmarketsPurpose
    account_scope: str
    event_id: str
    market_id: str
    observation_sha256: str
    rate_policy_ref: str
    max_requests_per_window: int
    rate_window_seconds: int
    execution_authorized: bool = False

    def __post_init__(self) -> None:
        if type(self.entitled) is not bool or type(self.purpose) is not SmarketsPurpose:
            raise SmarketsEntitlementError("invalid entitlement decision")
        _text(self.reason, "reason"); _text(self.account_scope, "account_scope")
        _text(self.event_id, "event_id"); _text(self.market_id, "market_id")
        _sha(self.observation_sha256, "observation_sha256"); _text(self.rate_policy_ref, "rate_policy_ref")
        _posint(self.max_requests_per_window, "max_requests_per_window"); _posint(self.rate_window_seconds, "rate_window_seconds")
        if self.execution_authorized is not False:
            raise SmarketsEntitlementError("entitlement evidence never grants execution authorization")


@dataclass(frozen=True, slots=True, weakref_slot=True)
class SmarketsSetupCostObservation:
    amount: Decimal
    currency: str
    billing_trigger: str
    observed_at: str
    source_document_sha256: str
    evidence_sha256: str
    refund_window_days: int | None = None
    refund_condition: str | None = None
    one_time: bool = True
    applies_per_bet: bool = False
    incurred_cost_authority: bool = False
    venue_id: str = VENUE_ID
    source_family: str = _COST_FAMILY
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.venue_id != VENUE_ID or self.source_family != _COST_FAMILY or self.schema_version != SCHEMA_VERSION:
            raise SmarketsEntitlementError("setup-cost schema/source mismatch")
        _money(self.amount, "amount"); _currency(self.currency); _text(self.billing_trigger, "billing_trigger")
        _dt_text(self.observed_at, "observed_at"); _sha(self.source_document_sha256, "source_document_sha256")
        if (self.refund_window_days is None) != (self.refund_condition is None):
            raise SmarketsEntitlementError("refund window and condition must be supplied together")
        if self.refund_window_days is not None:
            _posint(self.refund_window_days, "refund_window_days")
            _text(self.refund_condition, "refund_condition")
        if self.one_time is not True or self.applies_per_bet is not False or self.incurred_cost_authority is not False:
            raise SmarketsEntitlementError("setup cost must remain one-time, non-per-bet and non-incurred")
        _sha(self.evidence_sha256, "evidence_sha256")
        if self.evidence_sha256 != _digest(self.payload()):
            raise SmarketsEntitlementError("setup-cost evidence digest mismatch")

    def payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.smarkets_setup_cost_observation", "schema_version": self.schema_version,
            "venue_id": self.venue_id, "source_family": self.source_family, "amount": str(self.amount),
            "currency": self.currency, "billing_trigger": self.billing_trigger, "observed_at": self.observed_at,
            "source_document_sha256": self.source_document_sha256,
            "refund_window_days": self.refund_window_days, "refund_condition": self.refund_condition, "one_time": True,
            "applies_per_bet": False, "incurred_cost_authority": False,
        }


_ISSUED_ENT: dict[int, tuple[ref, str]] = {}
_ISSUED_COST: dict[int, tuple[ref, str]] = {}


def _register(registry: dict[int, tuple[ref, str]], value: object, digest: str) -> None:
    identity = id(value)
    def cleanup(dead: ref, expected: int = identity) -> None:
        current = registry.get(expected)
        if current is not None and current[0] is dead:
            registry.pop(expected, None)
    registry[identity] = (ref(value, cleanup), digest)


def _issued(registry: dict[int, tuple[ref, str]], value: object) -> str | None:
    current = registry.get(id(value))
    return current[1] if current is not None and current[0]() is value else None


def issue_smarkets_entitlement_observation(*, account_scope: str, approval_ref: str,
    approval_state: SmarketsApprovalState, approved_purposes: tuple[SmarketsPurpose, ...],
    all_events_approved: bool, allowed_event_ids: tuple[str, ...], all_markets_approved: bool,
    allowed_market_ids: tuple[str, ...], rate_policy_ref: str, max_requests_per_window: int,
    rate_window_seconds: int, observed_at: datetime, source_document_sha256: str
) -> SmarketsEntitlementObservation:
    account_scope = _text(account_scope, "account_scope"); approval_ref = _text(approval_ref, "approval_ref")
    if type(approval_state) is not SmarketsApprovalState:
        raise SmarketsEntitlementError("approval_state must be exact SmarketsApprovalState")
    if type(approved_purposes) is not tuple or any(type(x) is not SmarketsPurpose for x in approved_purposes):
        raise SmarketsEntitlementError("approved_purposes must contain exact SmarketsPurpose values")
    purposes = _purposes(approved_purposes)
    prohibited = _PROVIDER_PROHIBITED_PURPOSES.intersection(purposes)
    if prohibited:
        names = ",".join(sorted(item.value for item in prohibited))
        raise SmarketsEntitlementError(f"provider terms prohibit purpose under schema v1: {names}")
    events = _texts(allowed_event_ids, "allowed_event_ids")
    markets = _texts(allowed_market_ids, "allowed_market_ids")
    rate_policy_ref = _text(rate_policy_ref, "rate_policy_ref")
    _posint(max_requests_per_window, "max_requests_per_window"); _posint(rate_window_seconds, "rate_window_seconds")
    observed = _dt(_utc(observed_at, "observed_at")); source = _sha(source_document_sha256, "source_document_sha256")
    partial = {
        "schema": "autosport.smarkets_entitlement_observation", "schema_version": SCHEMA_VERSION,
        "venue_id": VENUE_ID, "source_family": _ENT_FAMILY, "account_scope": account_scope,
        "approval_ref": approval_ref, "approval_state": approval_state.value,
        "approved_purposes": [x.value for x in purposes], "all_events_approved": all_events_approved,
        "allowed_event_ids": list(events), "all_markets_approved": all_markets_approved,
        "allowed_market_ids": list(markets), "rate_policy_ref": rate_policy_ref,
        "max_requests_per_window": max_requests_per_window, "rate_window_seconds": rate_window_seconds,
        "observed_at": observed, "source_document_sha256": source,
    }
    digest = _digest(partial)
    result = SmarketsEntitlementObservation(account_scope, approval_ref, approval_state, purposes,
        all_events_approved, events, all_markets_approved, markets, rate_policy_ref,
        max_requests_per_window, rate_window_seconds, observed, source, digest)
    _register(_ISSUED_ENT, result, digest)
    return result


def _valid_issued_entitlement(v: SmarketsEntitlementObservation) -> bool:
    if type(v) is not SmarketsEntitlementObservation:
        return False
    try:
        current = _digest(v.payload())
    except SmarketsEntitlementError:
        return False
    return _issued(_ISSUED_ENT, v) == current == v.evidence_sha256


def evaluate_smarkets_entitlement(observation: SmarketsEntitlementObservation, *, account_scope: str,
    purpose: SmarketsPurpose, event_id: str, market_id: str, as_of: datetime, max_age_seconds: int
) -> SmarketsEntitlementDecision:
    if type(observation) is not SmarketsEntitlementObservation:
        raise SmarketsEntitlementError("observation must be exact SmarketsEntitlementObservation")
    if type(purpose) is not SmarketsPurpose:
        raise SmarketsEntitlementError("purpose must be exact SmarketsPurpose")
    account_scope = _text(account_scope, "account_scope")
    event_id = _text(event_id, "event_id"); market_id = _text(market_id, "market_id")
    as_of = _utc(as_of, "as_of"); _posint(max_age_seconds, "max_age_seconds")
    def out(ok: bool, reason: str) -> SmarketsEntitlementDecision:
        return SmarketsEntitlementDecision(ok, reason, purpose, observation.account_scope, event_id, market_id,
            observation.evidence_sha256, observation.rate_policy_ref, observation.max_requests_per_window,
            observation.rate_window_seconds)
    if not _valid_issued_entitlement(observation): return out(False, "UNISSUED_OR_MUTATED_EVIDENCE")
    if observation.account_scope != account_scope: return out(False, "ACCOUNT_SCOPE_MISMATCH")
    observed = _dt_text(observation.observed_at, "observed_at")
    if observed > as_of: return out(False, "FUTURE_EVIDENCE")
    if (as_of - observed).total_seconds() > max_age_seconds: return out(False, "STALE_EVIDENCE")
    if observation.approval_state is not SmarketsApprovalState.APPROVED:
        return out(False, f"APPROVAL_{observation.approval_state.value.upper()}")
    if purpose in _PROVIDER_PROHIBITED_PURPOSES: return out(False, "PURPOSE_PROHIBITED_BY_PROVIDER_TERMS")
    if purpose not in observation.approved_purposes: return out(False, "PURPOSE_NOT_APPROVED")
    if not observation.all_events_approved and event_id not in observation.allowed_event_ids:
        return out(False, "EVENT_NOT_APPROVED")
    if not observation.all_markets_approved and market_id not in observation.allowed_market_ids:
        return out(False, "MARKET_NOT_APPROVED")
    return out(True, "ENTITLEMENT_EVIDENCE_SATISFIED")


def resolve_smarkets_entitlement(observations: tuple[SmarketsEntitlementObservation, ...], *,
    account_scope: str, purpose: SmarketsPurpose, event_id: str, market_id: str,
    as_of: datetime, max_age_seconds: int
) -> SmarketsEntitlementDecision:
    if type(observations) is not tuple or not observations:
        raise SmarketsEntitlementError("observations must be a non-empty tuple")
    account_scope = _text(account_scope, "account_scope"); as_of = _utc(as_of, "as_of")
    candidates: list[SmarketsEntitlementObservation] = []; by_time: dict[str, str] = {}
    for item in observations:
        if type(item) is not SmarketsEntitlementObservation:
            raise SmarketsEntitlementError("observations must contain exact entitlement observations")
        if item.account_scope != account_scope or _dt_text(item.observed_at, "observed_at") > as_of: continue
        old = by_time.get(item.observed_at)
        if old is not None and old != item.evidence_sha256:
            raise SmarketsEntitlementError("conflicting entitlement observations share one timestamp")
        by_time[item.observed_at] = item.evidence_sha256; candidates.append(item)
    if not candidates:
        raise SmarketsEntitlementError("no entitlement observation visible for account scope")
    latest = max(candidates, key=lambda x: _dt_text(x.observed_at, "observed_at"))
    return evaluate_smarkets_entitlement(latest, account_scope=account_scope, purpose=purpose,
        event_id=event_id, market_id=market_id, as_of=as_of, max_age_seconds=max_age_seconds)


def issue_smarkets_setup_cost_observation(*, amount: Decimal, currency: str, billing_trigger: str,
    observed_at: datetime, source_document_sha256: str, refund_window_days: int | None = None,
    refund_condition: str | None = None) -> SmarketsSetupCostObservation:
    amount = _money(amount, "amount"); currency = _currency(currency); billing_trigger = _text(billing_trigger, "billing_trigger")
    observed = _dt(_utc(observed_at, "observed_at")); source = _sha(source_document_sha256, "source_document_sha256")
    if (refund_window_days is None) != (refund_condition is None):
        raise SmarketsEntitlementError("refund window and condition must be supplied together")
    if refund_window_days is not None:
        _posint(refund_window_days, "refund_window_days")
        refund_condition = _text(refund_condition, "refund_condition")
    partial = {
        "schema": "autosport.smarkets_setup_cost_observation", "schema_version": SCHEMA_VERSION,
        "venue_id": VENUE_ID, "source_family": _COST_FAMILY, "amount": str(amount), "currency": currency,
        "billing_trigger": billing_trigger, "observed_at": observed, "source_document_sha256": source,
        "refund_window_days": refund_window_days, "refund_condition": refund_condition,
        "one_time": True, "applies_per_bet": False, "incurred_cost_authority": False,
    }
    digest = _digest(partial)
    result = SmarketsSetupCostObservation(amount, currency, billing_trigger, observed, source, digest,
        refund_window_days, refund_condition)
    _register(_ISSUED_COST, result, digest)
    return result


def is_product_issued_smarkets_setup_cost(v: SmarketsSetupCostObservation) -> bool:
    if type(v) is not SmarketsSetupCostObservation: return False
    try: current = _digest(v.payload())
    except SmarketsEntitlementError: return False
    return _issued(_ISSUED_COST, v) == current == v.evidence_sha256
