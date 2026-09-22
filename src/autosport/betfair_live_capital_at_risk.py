"""Provider-backed live capital-at-risk for the supervised Betfair BACK/LIMIT seam."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from weakref import ref

from .betfair_account_readonly import (
    BetfairClearedOrderObservation,
    BetfairCurrentOrderObservation,
    BetfairExecutionReadbackEnvelope,
    BetfairReadOnlyError,
)
from .betfair_order_liability import (
    BetfairMarketBettingType,
    BetfairOrderSide,
    BetfairOrderType,
    derive_betfair_order_reserve,
)
from .real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionLedgerError,
    ExternalReceiptIdentity,
    RealExecutionLedger,
)
from .supervised_execution import (
    BoundSupervisedExecutionPlan,
    SupervisedExecutionError,
)


class BetfairLiveCapitalAtRiskError(RuntimeError):
    pass


class BetfairLiveCapitalAtRiskTruth(str, Enum):
    EXACT = "EXACT"
    UNKNOWN = "UNKNOWN"


class BetfairLiveCapitalAtRiskReason(str, Enum):
    CURRENT_ORDER = "CURRENT_ORDER"
    CLEARED_TERMINAL = "CLEARED_TERMINAL"
    NO_PROVIDER_ROW = "NO_PROVIDER_ROW"
    MULTIPLE_PROVIDER_ROWS = "MULTIPLE_PROVIDER_ROWS"
    CROSS_CALL_REVISION = "CROSS_CALL_REVISION"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    INCOMPLETE_PAGINATION = "INCOMPLETE_PAGINATION"
    UNSUPPORTED_ACTION_SEMANTICS = "UNSUPPORTED_ACTION_SEMANTICS"
    DURABLE_STATE_CONFLICT = "DURABLE_STATE_CONFLICT"
    ECONOMIC_INCONSISTENCY = "ECONOMIC_INCONSISTENCY"
    CURRENT_ORDER_NOT_CURRENT = "CURRENT_ORDER_NOT_CURRENT"
    CLEARED_MATCHED_EXPOSURE_UNRESOLVED = "CLEARED_MATCHED_EXPOSURE_UNRESOLVED"


_CURRENT_STATUSES = frozenset({"EXECUTABLE", "EXECUTION_COMPLETE"})
_CLEARED_STATUSES = ("SETTLED", "VOIDED", "LAPSED", "CANCELLED")
MAX_CURRENT_ORDER_EVIDENCE_AGE = timedelta(seconds=30)
_MAX_FUTURE_SKEW = timedelta(seconds=1)


def _decimal_text(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise BetfairLiveCapitalAtRiskError("economic decimal must be exact and finite")
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise BetfairLiveCapitalAtRiskError("economic decimal exponent is invalid")
    coefficient = "".join(str(digit) for digit in digits) or "0"
    if all(digit == "0" for digit in coefficient):
        return "0"
    while len(coefficient) > 1 and coefficient.endswith("0"):
        coefficient = coefficient[:-1]
        exponent += 1
    if exponent >= 0:
        body = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        body = (
            coefficient[:point] + "." + coefficient[point:]
            if point > 0
            else "0." + ("0" * (-point)) + coefficient
        )
    return ("-" if sign else "") + body


def _digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fraction_to_decimal(value: Fraction) -> Decimal:
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise BetfairLiveCapitalAtRiskError("live stake is not a finite Decimal")
    scale = max(twos, fives)
    scaled = (
        value.numerator
        * (2 ** (scale - twos))
        * (5 ** (scale - fives))
    )
    sign = int(scaled < 0)
    digits = tuple(int(char) for char in str(abs(scaled))) or (0,)
    return Decimal((sign, digits, -scale))


def _exact_add(left: Decimal, right: Decimal) -> Decimal:
    return _fraction_to_decimal(Fraction(left) + Fraction(right))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_timestamp(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BetfairLiveCapitalAtRiskError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_provider_observed_at(value: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairLiveCapitalAtRiskError(
            "provider observed_at must be non-empty timestamp text"
        )
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BetfairLiveCapitalAtRiskError(
            "provider observed_at must be ISO-8601 timestamp text"
        ) from exc
    return _utc_timestamp(parsed, "provider observed_at")


def _current_observation_is_current(
    observed_at: str,
    *,
    checked_at: datetime,
) -> bool:
    observed = _parse_provider_observed_at(observed_at)
    checked = _utc_timestamp(checked_at, "freshness checked_at")
    if observed > checked + _MAX_FUTURE_SKEW:
        return False
    return checked - observed <= MAX_CURRENT_ORDER_EVIDENCE_AGE


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairLiveCapitalAtRiskEvidence:
    truth: BetfairLiveCapitalAtRiskTruth
    reason: BetfairLiveCapitalAtRiskReason
    capital_at_risk: Decimal | None
    plan_id: str
    attempt_id: str
    attempt_state: AttemptState
    action_id: str
    provider_order_ref: str
    bet_id: str | None
    readback_observed_at: str
    provider_row_observed_at: str | None
    readback_request_scope_sha256: str
    readback_evidence_sha256: str
    ledger_snapshot_sha256: str
    execution_authority: bool = False

    def __post_init__(self) -> None:
        if self.execution_authority is not False:
            raise BetfairLiveCapitalAtRiskError(
                "live capital-at-risk never grants execution authority"
            )
        if type(self.truth) is not BetfairLiveCapitalAtRiskTruth:
            raise BetfairLiveCapitalAtRiskError("truth must be exact enum")
        if type(self.reason) is not BetfairLiveCapitalAtRiskReason:
            raise BetfairLiveCapitalAtRiskError("reason must be exact enum")
        if type(self.attempt_state) is not AttemptState:
            raise BetfairLiveCapitalAtRiskError("attempt_state must be exact enum")
        if self.provider_row_observed_at is not None:
            _parse_provider_observed_at(self.provider_row_observed_at)
        if self.truth is BetfairLiveCapitalAtRiskTruth.EXACT:
            if (
                type(self.capital_at_risk) is not Decimal
                or not self.capital_at_risk.is_finite()
                or self.capital_at_risk < 0
            ):
                raise BetfairLiveCapitalAtRiskError(
                    "EXACT risk requires finite non-negative Decimal"
                )
            if self.reason not in {
                BetfairLiveCapitalAtRiskReason.CURRENT_ORDER,
                BetfairLiveCapitalAtRiskReason.CLEARED_TERMINAL,
            }:
                raise BetfairLiveCapitalAtRiskError("EXACT risk has invalid reason")
            if (
                self.reason is BetfairLiveCapitalAtRiskReason.CURRENT_ORDER
                and self.provider_row_observed_at is None
            ):
                raise BetfairLiveCapitalAtRiskError(
                    "EXACT current-order risk requires provider row observation time"
                )
        elif self.capital_at_risk is not None:
            raise BetfairLiveCapitalAtRiskError("UNKNOWN risk cannot carry an amount")

    @property
    def evidence_id(self) -> str:
        return _digest(
            {
                "schema": "autosport.betfair_live_capital_at_risk",
                "schema_version": 1,
                "truth": self.truth.value,
                "reason": self.reason.value,
                "capital_at_risk": (
                    None
                    if self.capital_at_risk is None
                    else _decimal_text(self.capital_at_risk)
                ),
                "plan_id": self.plan_id,
                "attempt_id": self.attempt_id,
                "attempt_state": self.attempt_state.value,
                "action_id": self.action_id,
                "provider_order_ref": self.provider_order_ref,
                "bet_id": self.bet_id,
                "readback_observed_at": self.readback_observed_at,
                "provider_row_observed_at": self.provider_row_observed_at,
                "readback_request_scope_sha256": self.readback_request_scope_sha256,
                "readback_evidence_sha256": self.readback_evidence_sha256,
                "ledger_snapshot_sha256": self.ledger_snapshot_sha256,
                "execution_authority": False,
            }
        )

    def assert_authoritative(self) -> None:
        raise BetfairLiveCapitalAtRiskError(
            "live capital-at-risk evidence was not issued by canonical resolver"
        )


def _complete_pages(pages: tuple[object, ...]) -> bool:
    if not pages:
        return False
    expected_from = 0
    for index, page in enumerate(pages):
        orders = getattr(page, "orders", None)
        more = getattr(page, "more_available", None)
        if (
            getattr(page, "from_record", None) != expected_from
            or not isinstance(orders, tuple)
            or type(more) is not bool
        ):
            return False
        last = index == len(pages) - 1
        if (more and (last or not orders)) or (not more and not last):
            return False
        expected_from += len(orders)
    return True


def _complete_capture(readback: BetfairExecutionReadbackEnvelope) -> bool:
    if not _complete_pages(readback.current_pages):
        return False
    if tuple(status for status, _ in readback.cleared_pages_by_status) != _CLEARED_STATUSES:
        return False
    return all(_complete_pages(pages) for _, pages in readback.cleared_pages_by_status)


def _receipt_matches_attempt(
    *,
    saga,
    action: ExecutionAction,
    attempt_id: str,
    attempt_state: AttemptState,
    bet_id: str,
) -> bool:
    if attempt_state not in {AttemptState.ACCEPTED, AttemptState.PARTIAL}:
        return True
    try:
        identity = ExternalReceiptIdentity(
            action.bookmaker_id,
            action.account_id,
            bet_id,
        )
    except (TypeError, ValueError):
        return False
    return saga.receipts.get(identity) == attempt_id


def _row_identity_matches(
    row: BetfairCurrentOrderObservation | BetfairClearedOrderObservation,
    *,
    action: ExecutionAction,
    selection_id: int,
    provider_order_ref: str,
) -> bool:
    if (
        row.market_id != action.market_id
        or row.selection_id != selection_id
        or row.side != action.side
        or row.customer_order_ref != provider_order_ref
    ):
        return False
    return not (
        isinstance(row, BetfairClearedOrderObservation)
        and row.event_id is not None
        and row.event_id != action.event_id
    )


def resolve_betfair_live_capital_at_risk(
    ledger: RealExecutionLedger,
    bound: BoundSupervisedExecutionPlan,
    readback: BetfairExecutionReadbackEnvelope,
    *,
    attempt_id: str,
) -> BetfairLiveCapitalAtRiskEvidence:
    """Resolve current exposure; absence never authorizes zero."""
    if not isinstance(ledger, RealExecutionLedger):
        raise TypeError("ledger must be RealExecutionLedger")
    if not isinstance(bound, BoundSupervisedExecutionPlan):
        raise TypeError("bound must be BoundSupervisedExecutionPlan")
    if not isinstance(readback, BetfairExecutionReadbackEnvelope):
        raise TypeError("readback must be BetfairExecutionReadbackEnvelope")
    if type(attempt_id) is not str or not attempt_id.strip():
        raise BetfairLiveCapitalAtRiskError("attempt_id must be non-empty text")

    try:
        bound.verify_binding()
        before = ledger.verified_snapshot()
        saga = ledger.saga(bound.execution_plan.plan_id)
    except (SupervisedExecutionError, ExecutionLedgerError, KeyError, OSError) as exc:
        raise BetfairLiveCapitalAtRiskError(
            "durable supervised execution identity is unavailable"
        ) from exc
    if saga.plan_fingerprint != bound.execution_plan.fingerprint:
        raise BetfairLiveCapitalAtRiskError("durable plan fingerprint mismatch")
    action_id = saga.attempt_action_ids.get(attempt_id)
    state = saga.attempts.get(attempt_id)
    if action_id is None or state is None:
        raise BetfairLiveCapitalAtRiskError("attempt is outside durable plan")
    action = bound.action_for(action_id)
    try:
        provider_ref = ledger.provider_order_reference(
            attempt_id=attempt_id,
            provider_id=action.bookmaker_id,
        )
    except (ExecutionLedgerError, KeyError, OSError) as exc:
        raise BetfairLiveCapitalAtRiskError(
            "durable provider-order identity is unavailable"
        ) from exc
    if provider_ref is None:
        raise BetfairLiveCapitalAtRiskError(
            "attempt lacks durable provider order reference"
        )
    try:
        readback.assert_authoritative()
    except BetfairReadOnlyError as exc:
        raise BetfairLiveCapitalAtRiskError(
            "execution readback is not canonical provider evidence"
        ) from exc

    def finish(
        truth: BetfairLiveCapitalAtRiskTruth,
        reason: BetfairLiveCapitalAtRiskReason,
        amount: Decimal | None = None,
        bet_id: str | None = None,
        provider_row_observed_at: str | None = None,
    ) -> BetfairLiveCapitalAtRiskEvidence:
        try:
            after = ledger.verified_snapshot()
        except (ExecutionLedgerError, OSError) as exc:
            raise BetfairLiveCapitalAtRiskError(
                "execution ledger cannot be reverified"
            ) from exc
        if (
            before.sha256 != after.sha256
            or before.event_count != after.event_count
        ):
            raise BetfairLiveCapitalAtRiskError(
                "execution ledger changed during live-risk resolution"
            )
        return BetfairLiveCapitalAtRiskEvidence(
            truth=truth,
            reason=reason,
            capital_at_risk=amount,
            plan_id=bound.execution_plan.plan_id,
            attempt_id=attempt_id,
            attempt_state=state,
            action_id=action.action_id,
            provider_order_ref=provider_ref,
            bet_id=bet_id,
            readback_observed_at=readback.observed_at,
            provider_row_observed_at=provider_row_observed_at,
            readback_request_scope_sha256=readback.request_scope_sha256,
            readback_evidence_sha256=readback.evidence_sha256,
            ledger_snapshot_sha256=after.sha256,
        )

    if action.bookmaker_id != "betfair" or action.side != "BACK":
        return finish(
            BetfairLiveCapitalAtRiskTruth.UNKNOWN,
            BetfairLiveCapitalAtRiskReason.UNSUPPORTED_ACTION_SEMANTICS,
        )
    try:
        selection_id = int(action.selection_id)
    except (TypeError, ValueError):
        selection_id = -1
    if selection_id <= 0 or str(selection_id) != action.selection_id:
        return finish(
            BetfairLiveCapitalAtRiskTruth.UNKNOWN,
            BetfairLiveCapitalAtRiskReason.IDENTITY_MISMATCH,
        )
    if (
        readback.venue_id != action.bookmaker_id
        or readback.account_id != action.account_id
        or readback.action_id != action.action_id
        or readback.market_id != action.market_id
        or readback.market_event.event_id != action.event_id
        or readback.provider_order_ref != provider_ref
    ):
        return finish(
            BetfairLiveCapitalAtRiskTruth.UNKNOWN,
            BetfairLiveCapitalAtRiskReason.IDENTITY_MISMATCH,
        )
    if not _complete_capture(readback):
        return finish(
            BetfairLiveCapitalAtRiskTruth.UNKNOWN,
            BetfairLiveCapitalAtRiskReason.INCOMPLETE_PAGINATION,
        )

    current = tuple(order for page in readback.current_pages for order in page.orders)
    cleared = tuple(
        (status, order)
        for status, pages in readback.cleared_pages_by_status
        for page in pages
        for order in page.orders
    )
    for row in current:
        if not _row_identity_matches(
            row,
            action=action,
            selection_id=selection_id,
            provider_order_ref=provider_ref,
        ):
            return finish(
                BetfairLiveCapitalAtRiskTruth.UNKNOWN,
                BetfairLiveCapitalAtRiskReason.IDENTITY_MISMATCH,
                bet_id=row.bet_id,
            )
    for status, row in cleared:
        if (
            row.bet_status != status
            or not _row_identity_matches(
                row,
                action=action,
                selection_id=selection_id,
                provider_order_ref=provider_ref,
            )
        ):
            return finish(
                BetfairLiveCapitalAtRiskTruth.UNKNOWN,
                BetfairLiveCapitalAtRiskReason.IDENTITY_MISMATCH,
                bet_id=row.bet_id,
            )
    if current and cleared:
        return finish(
            BetfairLiveCapitalAtRiskTruth.UNKNOWN,
            BetfairLiveCapitalAtRiskReason.CROSS_CALL_REVISION,
        )
    if len(current) > 1 or len(cleared) > 1:
        return finish(
            BetfairLiveCapitalAtRiskTruth.UNKNOWN,
            BetfairLiveCapitalAtRiskReason.MULTIPLE_PROVIDER_ROWS,
        )
    if not current and not cleared:
        return finish(
            BetfairLiveCapitalAtRiskTruth.UNKNOWN,
            BetfairLiveCapitalAtRiskReason.NO_PROVIDER_ROW,
        )

    only_bet_id = current[0].bet_id if current else cleared[0][1].bet_id
    if not _receipt_matches_attempt(
        saga=saga,
        action=action,
        attempt_id=attempt_id,
        attempt_state=state,
        bet_id=only_bet_id,
    ):
        return finish(
            BetfairLiveCapitalAtRiskTruth.UNKNOWN,
            BetfairLiveCapitalAtRiskReason.IDENTITY_MISMATCH,
            bet_id=only_bet_id,
        )

    if current:
        row = current[0]
        if state not in {
            AttemptState.SUBMITTED,
            AttemptState.UNKNOWN,
            AttemptState.ACCEPTED,
            AttemptState.PARTIAL,
        }:
            return finish(
                BetfairLiveCapitalAtRiskTruth.UNKNOWN,
                BetfairLiveCapitalAtRiskReason.DURABLE_STATE_CONFLICT,
                bet_id=row.bet_id,
            )
        if not _current_observation_is_current(
            row.evidence.observed_at,
            checked_at=_utc_now(),
        ):
            return finish(
                BetfairLiveCapitalAtRiskTruth.UNKNOWN,
                BetfairLiveCapitalAtRiskReason.CURRENT_ORDER_NOT_CURRENT,
                bet_id=row.bet_id,
                provider_row_observed_at=row.evidence.observed_at,
            )
        if (
            row.status not in _CURRENT_STATUSES
            or row.price is None
            or row.requested_size is None
            or row.price != action.requested_odds
            or row.requested_size != action.requested_stake
            or (row.size_matched > 0 and row.average_price_matched <= 0)
            or (row.size_matched == 0 and row.average_price_matched != 0)
            or (row.status == "EXECUTABLE" and row.size_remaining <= 0)
            or (row.status == "EXECUTION_COMPLETE" and row.size_remaining != 0)
        ):
            return finish(
                BetfairLiveCapitalAtRiskTruth.UNKNOWN,
                BetfairLiveCapitalAtRiskReason.ECONOMIC_INCONSISTENCY,
                bet_id=row.bet_id,
            )
        live_size = _exact_add(row.size_matched, row.size_remaining)
        if live_size <= 0 or live_size > action.requested_stake:
            return finish(
                BetfairLiveCapitalAtRiskTruth.UNKNOWN,
                BetfairLiveCapitalAtRiskReason.ECONOMIC_INCONSISTENCY,
                bet_id=row.bet_id,
            )
        try:
            reserve = derive_betfair_order_reserve(
                side=BetfairOrderSide.BACK,
                market_betting_type=BetfairMarketBettingType.ODDS,
                order_type=BetfairOrderType.LIMIT,
                price=row.price,
                size=live_size,
            )
        except (ValueError, ArithmeticError):
            return finish(
                BetfairLiveCapitalAtRiskTruth.UNKNOWN,
                BetfairLiveCapitalAtRiskReason.ECONOMIC_INCONSISTENCY,
                bet_id=row.bet_id,
            )
        return finish(
            BetfairLiveCapitalAtRiskTruth.EXACT,
            BetfairLiveCapitalAtRiskReason.CURRENT_ORDER,
            reserve.reserve,
            row.bet_id,
            provider_row_observed_at=row.evidence.observed_at,
        )

    cleared_status, row = cleared[0]
    if state not in {AttemptState.ACCEPTED, AttemptState.PARTIAL}:
        return finish(
            BetfairLiveCapitalAtRiskTruth.UNKNOWN,
            BetfairLiveCapitalAtRiskReason.DURABLE_STATE_CONFLICT,
            bet_id=row.bet_id,
        )
    if cleared_status not in {"SETTLED", "VOIDED"}:
        return finish(
            BetfairLiveCapitalAtRiskTruth.UNKNOWN,
            BetfairLiveCapitalAtRiskReason.CLEARED_MATCHED_EXPOSURE_UNRESOLVED,
            bet_id=row.bet_id,
        )
    if (
        row.price_requested != action.requested_odds
        or row.size_settled > action.requested_stake
        or (row.size_settled > 0 and row.price_matched <= 0)
    ):
        return finish(
            BetfairLiveCapitalAtRiskTruth.UNKNOWN,
            BetfairLiveCapitalAtRiskReason.ECONOMIC_INCONSISTENCY,
            bet_id=row.bet_id,
        )
    return finish(
        BetfairLiveCapitalAtRiskTruth.EXACT,
        BetfairLiveCapitalAtRiskReason.CLEARED_TERMINAL,
        Decimal("0"),
        row.bet_id,
    )


def _install_authority() -> None:
    issued: dict[int, tuple[object, str]] = {}
    raw_resolve = resolve_betfair_live_capital_at_risk

    def authoritative_resolve(*args, **kwargs) -> BetfairLiveCapitalAtRiskEvidence:
        evidence = raw_resolve(*args, **kwargs)
        key = id(evidence)

        def forget(_weakref: object, *, identity: int = key) -> None:
            issued.pop(identity, None)

        issued[key] = (ref(evidence, forget), evidence.evidence_id)
        return evidence

    def assert_authoritative(self: BetfairLiveCapitalAtRiskEvidence) -> None:
        record = issued.get(id(self))
        if (
            record is None
            or record[0]() is not self
            or record[1] != self.evidence_id
        ):
            raise BetfairLiveCapitalAtRiskError(
                "live capital-at-risk evidence was not issued by canonical resolver"
            )
        if (
            self.truth is BetfairLiveCapitalAtRiskTruth.EXACT
            and self.reason is BetfairLiveCapitalAtRiskReason.CURRENT_ORDER
            and (
                self.provider_row_observed_at is None
                or not _current_observation_is_current(
                    self.provider_row_observed_at,
                    checked_at=_utc_now(),
                )
            )
        ):
            raise BetfairLiveCapitalAtRiskError(
                "live capital-at-risk current-order evidence is no longer current"
            )

    globals()["resolve_betfair_live_capital_at_risk"] = authoritative_resolve
    BetfairLiveCapitalAtRiskEvidence.assert_authoritative = assert_authoritative


_install_authority()
del _install_authority
