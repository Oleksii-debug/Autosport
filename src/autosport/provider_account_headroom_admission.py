"""Fail-closed provider-account headroom composition for supervised real execution.

This module composes canonical provider account acquisition, conservative durable
execution liability, and RealExecutionLedger snapshot-CAS. It intentionally
does not claim provider-side atomic funds reservation, provider write authority,
or whole-product real-money readiness.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from enum import Enum
import hashlib
import json
import threading
import weakref

from . import account_snapshot_acquisition as _account_acquisition
from .account_snapshot_acquisition import (
    AccountSnapshotAcquisitionError,
    AuthoritativeAccountSnapshot,
    assert_account_snapshot_acquisition_authoritative,
)
from .bookmaker_capability import BookmakerCapability
from .execution_capital_at_risk import (
    ExecutionCapitalAtRiskError,
    ExecutionCapitalAtRiskEvidence,
    resolve_execution_capital_at_risk,
)
from .real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionAttempt,
    ExecutionStateError,
    RealExecutionLedger,
    VerifiedExecutionPlanView,
)


_VERIFIED_SNAPSHOT = RealExecutionLedger.verified_snapshot
_VERIFIED_EXECUTION_VIEW = RealExecutionLedger.verified_execution_view
_BEGIN_ATTEMPT = RealExecutionLedger.begin_attempt
_ATTEMPT_STATE = RealExecutionLedger.attempt_state
_VERIFIED_SNAPSHOT_CODE = getattr(_VERIFIED_SNAPSHOT, "__code__", None)
_VERIFIED_EXECUTION_VIEW_CODE = getattr(_VERIFIED_EXECUTION_VIEW, "__code__", None)
_BEGIN_ATTEMPT_CODE = getattr(_BEGIN_ATTEMPT, "__code__", None)
_ATTEMPT_STATE_CODE = getattr(_ATTEMPT_STATE, "__code__", None)
_ASSERT_ACCOUNT_SNAPSHOT_AUTHORITY = assert_account_snapshot_acquisition_authoritative
_ASSERT_ACCOUNT_SNAPSHOT_AUTHORITY_CODE = getattr(
    _ASSERT_ACCOUNT_SNAPSHOT_AUTHORITY,
    "__code__",
    None,
)


class ProviderAccountHeadroomError(RuntimeError):
    """Base error for provider-account capital-axis admission evidence."""


class ProviderAccountHeadroomStale(ProviderAccountHeadroomError):
    """An input generation moved before a new internal reservation committed."""


class ProviderAccountHeadroomUnsupported(ProviderAccountHeadroomError):
    """Current canonical evidence cannot support this account/action shape."""


class HeadroomDecision(str, Enum):
    SUFFICIENT_LOWER_BOUND = "SUFFICIENT_LOWER_BOUND"
    INSUFFICIENT_UPPER_BOUND = "INSUFFICIENT_UPPER_BOUND"
    WAIT_COVERAGE = "WAIT_COVERAGE"


def _canonical_account_snapshot_authority(
    *,
    _assert=_ASSERT_ACCOUNT_SNAPSHOT_AUTHORITY,
    _assert_code=_ASSERT_ACCOUNT_SNAPSHOT_AUTHORITY_CODE,
):
    live_module = getattr(
        _account_acquisition,
        "assert_account_snapshot_acquisition_authoritative",
        None,
    )
    live_alias = globals().get("assert_account_snapshot_acquisition_authoritative")
    if (
        live_module is not _assert
        or live_alias is not _assert
        or getattr(_assert, "__code__", None) is not _assert_code
    ):
        raise ProviderAccountHeadroomError(
            "canonical account snapshot headroom authority changed"
        )
    return _assert


def _canonical_ledger_dispatch(
    *,
    _snapshot=_VERIFIED_SNAPSHOT,
    _snapshot_code=_VERIFIED_SNAPSHOT_CODE,
    _view=_VERIFIED_EXECUTION_VIEW,
    _view_code=_VERIFIED_EXECUTION_VIEW_CODE,
    _begin=_BEGIN_ATTEMPT,
    _begin_code=_BEGIN_ATTEMPT_CODE,
    _state=_ATTEMPT_STATE,
    _state_code=_ATTEMPT_STATE_CODE,
):
    expected = (_snapshot, _view, _begin, _state)
    live_class = (
        vars(RealExecutionLedger).get("verified_snapshot"),
        vars(RealExecutionLedger).get("verified_execution_view"),
        vars(RealExecutionLedger).get("begin_attempt"),
        vars(RealExecutionLedger).get("attempt_state"),
    )
    live_aliases = (
        globals().get("_VERIFIED_SNAPSHOT"),
        globals().get("_VERIFIED_EXECUTION_VIEW"),
        globals().get("_BEGIN_ATTEMPT"),
        globals().get("_ATTEMPT_STATE"),
    )
    expected_codes = (
        _snapshot_code,
        _view_code,
        _begin_code,
        _state_code,
    )
    if (
        live_class != expected
        or live_aliases != expected
        or tuple(getattr(item, "__code__", None) for item in expected)
        != expected_codes
    ):
        raise ProviderAccountHeadroomError(
            "canonical execution ledger headroom authority changed"
        )
    return expected


_PRODUCT_MAX_ACCOUNT_SNAPSHOT_AGE = timedelta(seconds=30)
_SCHEMA_VERSION = 1
_ZERO = Decimal("0")


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderAccountHeadroomError(f"{field} must be non-empty trimmed text")
    return value


def _sha(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ProviderAccountHeadroomError(
            f"{field} must be lowercase 64-character SHA-256 text"
        )
    return text


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProviderAccountHeadroomError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderAccountHeadroomError(f"{field} must include timezone offset")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ProviderAccountHeadroomError(f"{field} must be exact finite Decimal")
    if value < 0 or (positive and value <= 0):
        qualifier = "positive" if positive else "non-negative"
        raise ProviderAccountHeadroomError(f"{field} must be {qualifier}")
    return value


def _add(left: Decimal, right: Decimal) -> Decimal:
    _decimal(left, "left")
    _decimal(right, "right")
    required = max(
        len(left.as_tuple().digits) + abs(int(left.as_tuple().exponent)),
        len(right.as_tuple().digits) + abs(int(right.as_tuple().exponent)),
        64,
    ) + 4
    if required > 4096:
        raise ProviderAccountHeadroomUnsupported(
            "capital scale exceeds bounded exact arithmetic"
        )
    with localcontext() as context:
        context.prec = required
        result = left + right
    if not result.is_finite():
        raise ProviderAccountHeadroomError("capital addition became non-finite")
    return result


def _subtract_floor_zero(left: Decimal, right: Decimal) -> Decimal:
    _decimal(left, "left")
    _decimal(right, "right")
    required = max(
        len(left.as_tuple().digits) + abs(int(left.as_tuple().exponent)),
        len(right.as_tuple().digits) + abs(int(right.as_tuple().exponent)),
        64,
    ) + 4
    if required > 4096:
        raise ProviderAccountHeadroomUnsupported(
            "capital scale exceeds bounded exact arithmetic"
        )
    with localcontext() as context:
        context.prec = required
        result = left - right
    if not result.is_finite():
        raise ProviderAccountHeadroomError("capital subtraction became non-finite")
    return max(_ZERO, result)


def _decimal_text(value: Decimal) -> str:
    _decimal(value, "decimal")
    if value.is_zero():
        return "0"
    sign, digits, exponent = value.as_tuple()
    raw = list(digits)
    exp = int(exponent)
    while raw and raw[-1] == 0:
        raw.pop()
        exp += 1
    coefficient = "".join(str(digit) for digit in raw) or "0"
    return ("-" if sign else "") + coefficient + f"e{exp}"


def _canonical_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc_now(
    *,
    _datetime_now=datetime.now,
    _utc=timezone.utc,
) -> datetime:
    return _datetime_now(_utc)


def _read_headroom_utc_now(
    *,
    _clock=_utc_now,
    _clock_code=getattr(_utc_now, "__code__", None),
) -> datetime:
    live = globals().get("_utc_now")
    if (
        live is not _clock
        or getattr(live, "__code__", None) is not _clock_code
    ):
        raise ProviderAccountHeadroomError(
            "canonical provider-account headroom clock authority changed"
        )
    value = _clock()
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ProviderAccountHeadroomError(
            "canonical provider-account headroom clock returned invalid time"
        )
    return value.astimezone(timezone.utc)


def _ledger_plan_ids(snapshot_payload: bytes) -> tuple[str, ...]:
    """Enumerate plans only from bytes already verified by RealExecutionLedger."""
    if type(snapshot_payload) is not bytes:
        raise ProviderAccountHeadroomError("verified ledger payload must be exact bytes")
    plan_ids: set[str] = set()
    for raw_line in snapshot_payload.splitlines():
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderAccountHeadroomError(
                "verified ledger payload could not be enumerated"
            ) from exc
        if type(event) is not dict:
            raise ProviderAccountHeadroomError("verified ledger event must be an object")
        plan_id = event.get("plan_id")
        if type(plan_id) is not str or not plan_id:
            raise ProviderAccountHeadroomError(
                "verified ledger event lacks canonical plan identity"
            )
        plan_ids.add(plan_id)
    return tuple(sorted(plan_ids))


def _find_action(
    view: VerifiedExecutionPlanView,
    action_id: str,
) -> ExecutionAction:
    matches = tuple(action for action in view.plan.actions if action.action_id == action_id)
    if len(matches) != 1:
        raise ProviderAccountHeadroomUnsupported(
            "target action is not uniquely present in the canonical execution plan"
        )
    return matches[0]


def _classify(
    lower_headroom: Decimal,
    upper_headroom: Decimal,
    proposed_liability: Decimal,
) -> HeadroomDecision:
    _decimal(lower_headroom, "lower_headroom")
    _decimal(upper_headroom, "upper_headroom")
    _decimal(proposed_liability, "proposed_liability", positive=True)
    if lower_headroom >= proposed_liability:
        return HeadroomDecision.SUFFICIENT_LOWER_BOUND
    if upper_headroom < proposed_liability:
        return HeadroomDecision.INSUFFICIENT_UPPER_BOUND
    return HeadroomDecision.WAIT_COVERAGE


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ProviderAccountHeadroomAssessment:
    provider_id: str
    account_id: str
    currency: str
    acquisition_id: str
    acquisition_snapshot_sha256: str
    acquired_at: str
    balance_observed_at: str
    expires_at: str
    ledger_snapshot_sha256: str
    ledger_event_count: int
    plan_id: str
    action_id: str
    action_fingerprint: str
    proposed_liability: Decimal
    provider_available_to_bet: Decimal
    definitely_unreflected_product_liability: Decimal
    unknown_reflection_product_liability: Decimal
    lower_headroom: Decimal
    upper_headroom: Decimal
    decision: HeadroomDecision
    evidence_sha256: str
    provider_atomicity_proven: bool = False
    provider_balance_generation_cas_proven: bool = False
    execution_authority: bool = False
    real_money_readiness: bool = False
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field in (
            "provider_id",
            "account_id",
            "currency",
            "acquisition_id",
            "acquired_at",
            "balance_observed_at",
            "expires_at",
            "plan_id",
            "action_id",
        ):
            _text(getattr(self, field), field)
        for field in (
            "acquisition_snapshot_sha256",
            "ledger_snapshot_sha256",
            "action_fingerprint",
            "evidence_sha256",
        ):
            _sha(getattr(self, field), field)
        if type(self.ledger_event_count) is not int or self.ledger_event_count < 0:
            raise ProviderAccountHeadroomError(
                "ledger_event_count must be non-negative exact int"
            )
        if type(self.decision) is not HeadroomDecision:
            raise ProviderAccountHeadroomError("decision must be exact HeadroomDecision")
        for field in (
            "proposed_liability",
            "provider_available_to_bet",
            "definitely_unreflected_product_liability",
            "unknown_reflection_product_liability",
            "lower_headroom",
            "upper_headroom",
        ):
            _decimal(
                getattr(self, field),
                field,
                positive=(field == "proposed_liability"),
            )
        if self.lower_headroom > self.upper_headroom:
            raise ProviderAccountHeadroomError(
                "lower_headroom cannot exceed upper_headroom"
            )
        expected_upper = _subtract_floor_zero(
            self.provider_available_to_bet,
            self.definitely_unreflected_product_liability,
        )
        expected_lower = _subtract_floor_zero(
            expected_upper,
            self.unknown_reflection_product_liability,
        )
        if self.upper_headroom != expected_upper or self.lower_headroom != expected_lower:
            raise ProviderAccountHeadroomError(
                "headroom bounds do not match conservative liability lattice"
            )
        expected_decision = _classify(
            self.lower_headroom,
            self.upper_headroom,
            self.proposed_liability,
        )
        if self.decision is not expected_decision:
            raise ProviderAccountHeadroomError(
                "headroom decision does not match conservative bounds"
            )
        if (
            self.provider_atomicity_proven is not False
            or self.provider_balance_generation_cas_proven is not False
            or self.execution_authority is not False
            or self.real_money_readiness is not False
        ):
            raise ProviderAccountHeadroomError(
                "headroom assessment cannot claim provider atomicity, balance-head CAS, "
                "execution authority, or real-money readiness"
            )
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise ProviderAccountHeadroomError("unsupported headroom assessment schema")
        acquired_time = _timestamp(self.acquired_at, "acquired_at")
        balance_time = _timestamp(self.balance_observed_at, "balance_observed_at")
        if balance_time > acquired_time:
            raise ProviderAccountHeadroomError(
                "provider balance observation cannot postdate product acquisition"
            )
        if _timestamp(self.expires_at, "expires_at") <= balance_time:
            raise ProviderAccountHeadroomError(
                "assessment expiry must follow provider balance observation"
            )


@dataclass(frozen=True, slots=True)
class ProductInternalHeadroomReservation:
    assessment_sha256: str
    attempt_id: str
    attempt_fingerprint: str
    reserved_at: str
    post_reservation_ledger_sha256: str
    post_reservation_event_count: int
    product_internal_reservation_proven: bool = True
    provider_atomicity_proven: bool = False
    execution_authority: bool = False
    real_money_readiness: bool = False

    def __post_init__(self) -> None:
        for field in (
            "assessment_sha256",
            "attempt_fingerprint",
            "post_reservation_ledger_sha256",
        ):
            _sha(getattr(self, field), field)
        _text(self.attempt_id, "attempt_id")
        _timestamp(self.reserved_at, "reserved_at")
        if (
            type(self.post_reservation_event_count) is not int
            or self.post_reservation_event_count < 0
        ):
            raise ProviderAccountHeadroomError(
                "post_reservation_event_count must be non-negative exact int"
            )
        if self.product_internal_reservation_proven is not True:
            raise ProviderAccountHeadroomError(
                "product_internal_reservation_proven must be exactly true"
            )
        if (
            self.provider_atomicity_proven is not False
            or self.execution_authority is not False
            or self.real_money_readiness is not False
        ):
            raise ProviderAccountHeadroomError(
                "internal reservation cannot claim provider atomicity, execution authority, "
                "or real-money readiness"
            )


_ISSUED_LOCK = threading.RLock()
_ISSUED: dict[
    int,
    tuple[weakref.ReferenceType[ProviderAccountHeadroomAssessment], str],
] = {}


def _assessment_payload(value: ProviderAccountHeadroomAssessment) -> dict[str, object]:
    return {
        "schema": "autosport.provider_account_headroom_assessment",
        "schema_version": value.schema_version,
        "provider_id": value.provider_id,
        "account_id": value.account_id,
        "currency": value.currency,
        "acquisition_id": value.acquisition_id,
        "acquisition_snapshot_sha256": value.acquisition_snapshot_sha256,
        "acquired_at": value.acquired_at,
        "balance_observed_at": value.balance_observed_at,
        "expires_at": value.expires_at,
        "ledger_snapshot_sha256": value.ledger_snapshot_sha256,
        "ledger_event_count": value.ledger_event_count,
        "plan_id": value.plan_id,
        "action_id": value.action_id,
        "action_fingerprint": value.action_fingerprint,
        "proposed_liability": _decimal_text(value.proposed_liability),
        "provider_available_to_bet": _decimal_text(value.provider_available_to_bet),
        "definitely_unreflected_product_liability": _decimal_text(
            value.definitely_unreflected_product_liability
        ),
        "unknown_reflection_product_liability": _decimal_text(
            value.unknown_reflection_product_liability
        ),
        "lower_headroom": _decimal_text(value.lower_headroom),
        "upper_headroom": _decimal_text(value.upper_headroom),
        "decision": value.decision.value,
        "provider_atomicity_proven": value.provider_atomicity_proven,
        "provider_balance_generation_cas_proven": (
            value.provider_balance_generation_cas_proven
        ),
        "execution_authority": value.execution_authority,
        "real_money_readiness": value.real_money_readiness,
    }


def _assessment_digest(value: ProviderAccountHeadroomAssessment) -> str:
    return _canonical_digest(_assessment_payload(value))


def _issue_assessment(value: ProviderAccountHeadroomAssessment) -> None:
    key = id(value)

    def cleanup(
        reference: weakref.ReferenceType[ProviderAccountHeadroomAssessment],
    ) -> None:
        with _ISSUED_LOCK:
            current = _ISSUED.get(key)
            if current is not None and current[0] is reference:
                _ISSUED.pop(key, None)

    reference = weakref.ref(value, cleanup)
    with _ISSUED_LOCK:
        _ISSUED[key] = (reference, value.evidence_sha256)


def _assert_issued(value: ProviderAccountHeadroomAssessment) -> None:
    if type(value) is not ProviderAccountHeadroomAssessment:
        raise ProviderAccountHeadroomError(
            "assessment must be exact ProviderAccountHeadroomAssessment"
        )
    with _ISSUED_LOCK:
        current = _ISSUED.get(id(value))
        if (
            current is None
            or current[0]() is not value
            or current[1] != value.evidence_sha256
        ):
            raise ProviderAccountHeadroomError(
                "headroom assessment was not canonically issued"
            )
    if _assessment_digest(value) != value.evidence_sha256:
        raise ProviderAccountHeadroomError("headroom assessment identity changed")


def _require_live_balance(
    acquired: AuthoritativeAccountSnapshot,
    *,
    now: datetime,
) -> tuple[Decimal, str, datetime, datetime]:
    if type(acquired) is not AuthoritativeAccountSnapshot:
        raise ProviderAccountHeadroomError(
            "account evidence must be exact AuthoritativeAccountSnapshot"
        )
    try:
        assert_live = _canonical_account_snapshot_authority()
        assert_live(acquired)
    except AccountSnapshotAcquisitionError as exc:
        raise ProviderAccountHeadroomError(
            "account snapshot lacks live canonical provider-origin authority"
        ) from exc
    receipt = acquired.receipt
    snapshot = acquired.snapshot
    if BookmakerCapability.BALANCE_READ.value not in receipt.requested_capabilities:
        raise ProviderAccountHeadroomUnsupported(
            "account acquisition does not bind BALANCE_READ"
        )
    balance = snapshot.balance
    if balance is None:
        raise ProviderAccountHeadroomUnsupported(
            "live account acquisition lacks provider available-balance evidence"
        )
    if (
        receipt.venue_id != snapshot.profile.venue_id
        or receipt.account_id != snapshot.profile.account_id
        or balance.venue_id != receipt.venue_id
        or balance.account_id != receipt.account_id
    ):
        raise ProviderAccountHeadroomError(
            "account acquisition provider/account scope is inconsistent"
        )
    if balance.currency != balance.currency.upper() or len(balance.currency) != 3:
        raise ProviderAccountHeadroomUnsupported(
            "provider account currency is not canonical three-letter code"
        )
    acquired_at = _timestamp(receipt.acquired_at, "acquired_at")
    balance_observed_at = _timestamp(balance.observed_at, "balance observed_at")
    if acquired_at > now:
        raise ProviderAccountHeadroomStale("account acquisition is from the future")
    if balance_observed_at > acquired_at:
        raise ProviderAccountHeadroomError(
            "provider balance observation postdates product acquisition receipt"
        )
    if balance_observed_at > now:
        raise ProviderAccountHeadroomStale(
            "provider balance observation is from the future"
        )
    if now - balance_observed_at > _PRODUCT_MAX_ACCOUNT_SNAPSHOT_AGE:
        raise ProviderAccountHeadroomStale(
            "provider balance observation exceeds product headroom freshness ceiling"
        )
    return (
        _decimal(balance.available_balance, "available_to_bet"),
        balance.currency,
        acquired_at,
        balance_observed_at,
    )


def _resolve_account_liability_lattice(
    ledger: RealExecutionLedger,
    *,
    provider_id: str,
    account_id: str,
    expected_snapshot_sha256: str,
    expected_event_count: int,
) -> tuple[Decimal, Decimal]:
    """Return (definitely-unreflected, unknown-reflection) liability.

    RESERVED is definitely product-side only: it has not crossed the provider
    boundary and therefore must be subtracted from available-to-bet. All
    externally plausible non-RESERVED liability remains UNKNOWN with respect to
    inclusion in the exact provider balance observation until a separate
    canonical causal-coverage authority proves otherwise.
    """
    verified_snapshot, verified_execution_view, _, _ = _canonical_ledger_dispatch()
    start = verified_snapshot(ledger)
    if (
        start.sha256 != expected_snapshot_sha256
        or start.event_count != expected_event_count
    ):
        raise ProviderAccountHeadroomStale(
            "execution ledger changed before account-liability resolution"
        )
    definitely_unreflected = _ZERO
    unknown_reflection = _ZERO

    for plan_id in _ledger_plan_ids(start.payload):
        try:
            view = verified_execution_view(ledger, plan_id)
            relevant_attempts = tuple(
                attempt
                for attempt in view.attempts
                if (attempt.action.bookmaker_id, attempt.action.account_id)
                == (provider_id, account_id)
            )
            if not relevant_attempts:
                continue
            capital: ExecutionCapitalAtRiskEvidence = resolve_execution_capital_at_risk(
                ledger,
                plan_id,
            )
            capital.assert_issued_current(ledger)
        except ExecutionCapitalAtRiskError as exc:
            raise ProviderAccountHeadroomUnsupported(
                "canonical capital-at-risk evidence cannot cover the full execution ledger"
            ) from exc
        if (
            view.snapshot_sha256 != expected_snapshot_sha256
            or view.event_count != expected_event_count
            or capital.snapshot_sha256 != expected_snapshot_sha256
            or capital.event_count != expected_event_count
        ):
            raise ProviderAccountHeadroomStale(
                "execution ledger changed during account-liability resolution"
            )
        risk_by_attempt = {item.attempt_id: item for item in capital.attempts}
        if len(risk_by_attempt) != len(capital.attempts):
            raise ProviderAccountHeadroomError(
                "canonical capital-at-risk evidence duplicated attempt identity"
            )
        for attempt in relevant_attempts:
            action = attempt.action
            risk = risk_by_attempt.get(attempt.attempt.attempt_id)
            if risk is None:
                raise ProviderAccountHeadroomUnsupported(
                    "account attempt is absent from canonical capital-at-risk evidence"
                )
            if attempt.state is AttemptState.RESERVED:
                definitely_unreflected = _add(
                    definitely_unreflected,
                    risk.requested_capital_at_limit,
                )
            else:
                unknown_reflection = _add(
                    unknown_reflection,
                    risk.max_plausible_capital_at_risk,
                )

    finish = verified_snapshot(ledger)
    if (
        finish.sha256 != expected_snapshot_sha256
        or finish.event_count != expected_event_count
    ):
        raise ProviderAccountHeadroomStale(
            "execution ledger changed during complete account-liability scan"
        )
    return definitely_unreflected, unknown_reflection


def assess_provider_account_headroom(
    ledger: RealExecutionLedger,
    acquired: AuthoritativeAccountSnapshot,
    *,
    plan_id: str,
    action_id: str,
) -> ProviderAccountHeadroomAssessment:
    """Issue conservative capital-axis evidence from exact canonical truth."""
    if type(ledger) is not RealExecutionLedger:
        raise TypeError("ledger must be exact RealExecutionLedger")
    plan_id = _text(plan_id, "plan_id")
    action_id = _text(action_id, "action_id")
    verified_snapshot, verified_execution_view, _, _ = _canonical_ledger_dispatch()
    now = _read_headroom_utc_now()
    available, currency, acquired_at, balance_observed_at = _require_live_balance(
        acquired,
        now=now,
    )

    snapshot = verified_snapshot(ledger)
    try:
        target_view = verified_execution_view(ledger, plan_id)
    except KeyError as exc:
        raise ProviderAccountHeadroomUnsupported(
            "target execution plan is not durably reserved"
        ) from exc
    if (
        target_view.snapshot_sha256 != snapshot.sha256
        or target_view.event_count != snapshot.event_count
    ):
        raise ProviderAccountHeadroomStale(
            "execution ledger changed while resolving target action"
        )
    action = _find_action(target_view, action_id)
    if action.bookmaker_id != "betfair" or action.side != "BACK":
        raise ProviderAccountHeadroomUnsupported(
            "current provider-account headroom admission supports Betfair BACK only"
        )
    if (action.bookmaker_id, action.account_id) != (
        acquired.receipt.venue_id,
        acquired.receipt.account_id,
    ):
        raise ProviderAccountHeadroomUnsupported(
            "target action provider/account mismatches live balance acquisition"
        )
    proposed = _decimal(
        action.requested_stake,
        "proposed Betfair BACK liability",
        positive=True,
    )

    definitely_unreflected, unknown_reflection = _resolve_account_liability_lattice(
        ledger,
        provider_id=action.bookmaker_id,
        account_id=action.account_id,
        expected_snapshot_sha256=snapshot.sha256,
        expected_event_count=snapshot.event_count,
    )
    upper = _subtract_floor_zero(available, definitely_unreflected)
    lower = _subtract_floor_zero(upper, unknown_reflection)
    decision = _classify(lower, upper, proposed)

    action_fingerprint = _canonical_digest(
        {
            "plan_fingerprint": target_view.plan_fingerprint,
            "action": action.to_dict(),
            "currency": currency,
        }
    )
    action_expiry = _timestamp(action.expires_at, "action expires_at")
    freshness_expiry = balance_observed_at + _PRODUCT_MAX_ACCOUNT_SNAPSHOT_AGE
    expiry = min(action_expiry, freshness_expiry)
    if now >= expiry:
        raise ProviderAccountHeadroomStale(
            "target quote or provider-account observation already expired"
        )

    provisional = ProviderAccountHeadroomAssessment(
        provider_id=action.bookmaker_id,
        account_id=action.account_id,
        currency=currency,
        acquisition_id=acquired.receipt.acquisition_id,
        acquisition_snapshot_sha256=acquired.receipt.snapshot_sha256,
        acquired_at=acquired.receipt.acquired_at,
        balance_observed_at=acquired.snapshot.balance.observed_at,
        expires_at=expiry.isoformat(),
        ledger_snapshot_sha256=snapshot.sha256,
        ledger_event_count=snapshot.event_count,
        plan_id=plan_id,
        action_id=action_id,
        action_fingerprint=action_fingerprint,
        proposed_liability=proposed,
        provider_available_to_bet=available,
        definitely_unreflected_product_liability=definitely_unreflected,
        unknown_reflection_product_liability=unknown_reflection,
        lower_headroom=lower,
        upper_headroom=upper,
        decision=decision,
        evidence_sha256="0" * 64,
    )
    assessment = ProviderAccountHeadroomAssessment(
        **{
            field: getattr(provisional, field)
            for field in provisional.__dataclass_fields__
            if field != "evidence_sha256"
        },
        evidence_sha256=_assessment_digest(provisional),
    )
    final_snapshot = verified_snapshot(ledger)
    if (
        final_snapshot.sha256 != snapshot.sha256
        or final_snapshot.event_count != snapshot.event_count
    ):
        raise ProviderAccountHeadroomStale(
            "execution ledger changed before headroom assessment issuance"
        )
    _issue_assessment(assessment)
    return assessment


def reserve_observed_provider_headroom(
    ledger: RealExecutionLedger,
    acquired: AuthoritativeAccountSnapshot,
    assessment: ProviderAccountHeadroomAssessment,
    *,
    attempt_id: str,
) -> ProductInternalHeadroomReservation:
    """Atomically consume product-internal headroom against exact ledger bytes.

    This does not place an order. The provider account can still change
    out-of-band after the observation; that uncertainty remains explicit.
    """
    if type(ledger) is not RealExecutionLedger:
        raise TypeError("ledger must be exact RealExecutionLedger")
    verified_snapshot, _, begin_attempt, attempt_state = _canonical_ledger_dispatch()
    _assert_issued(assessment)
    attempt_id = _text(attempt_id, "attempt_id")
    if type(acquired) is not AuthoritativeAccountSnapshot:
        raise ProviderAccountHeadroomError(
            "account evidence must be exact AuthoritativeAccountSnapshot"
        )
    if (
        acquired.receipt.acquisition_id != assessment.acquisition_id
        or acquired.receipt.snapshot_sha256 != assessment.acquisition_snapshot_sha256
    ):
        raise ProviderAccountHeadroomStale(
            "account acquisition changed after headroom assessment"
        )

    try:
        attempt_state(ledger, attempt_id)
    except KeyError:
        prior_exists = False
    else:
        prior_exists = True

    if not prior_exists:
        if assessment.decision is not HeadroomDecision.SUFFICIENT_LOWER_BOUND:
            raise ProviderAccountHeadroomUnsupported(
                "new internal reservation requires proven sufficient lower-bound headroom"
            )
        now = _read_headroom_utc_now()
        _require_live_balance(acquired, now=now)
        if now >= _timestamp(assessment.expires_at, "assessment expires_at"):
            raise ProviderAccountHeadroomStale(
                "headroom assessment expired before reservation"
            )

    try:
        # Canonical begin_attempt resolves an exact existing attempt before its
        # stale-snapshot fence. That ordering is essential for crash/restart
        # idempotency: identity resolution consumes no new provider headroom.
        attempt: ExecutionAttempt = begin_attempt(
            ledger,
            plan_id=assessment.plan_id,
            action_id=assessment.action_id,
            attempt_id=attempt_id,
            expected_snapshot_sha256=assessment.ledger_snapshot_sha256,
        )
    except ExecutionStateError as exc:
        raise ProviderAccountHeadroomStale(
            "execution ledger changed; recompute provider-account headroom"
        ) from exc

    post = verified_snapshot(ledger)
    return ProductInternalHeadroomReservation(
        assessment_sha256=assessment.evidence_sha256,
        attempt_id=attempt.attempt_id,
        attempt_fingerprint=attempt.effect_fingerprint,
        reserved_at=attempt.reserved_at,
        post_reservation_ledger_sha256=post.sha256,
        post_reservation_event_count=post.event_count,
    )
