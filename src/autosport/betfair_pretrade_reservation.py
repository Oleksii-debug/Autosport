"""Durable Betfair pre-trade exposure reservation.

This module composes the canonical product-issued Betfair funds precheck with the
canonical RealExecutionLedger. It does not acquire provider funds and does not
write to Betfair.

Before a provider mutation, one exact execution attempt must atomically reserve
its full worst-case incremental exposure. SUBMITTED, UNKNOWN, ACCEPTED and
PARTIAL attempts remain fully reserved. Capital is released here only when the
execution ledger proves REJECTED or RECONCILED_NOT_FOUND. A provider funds
snapshot therefore cannot be double-spent by concurrent Autosport workers.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any

from .betfair_account_funds_precheck import (
    BetfairAccountFundsPrecheck,
    BetfairAccountFundsPrecheckError,
    require_authoritative_funds_precheck,
)
from .real_execution_ledger import (
    AttemptState,
    EventType,
    ExecutionAction,
    ExecutionLedgerError,
    RealExecutionLedger,
)


_SCHEMA_VERSION = 1
_VENUE_ID = "betfair"
_ZERO = Decimal("0")
_ACTIVE_STATES = frozenset(
    {
        AttemptState.RESERVED,
        AttemptState.SUBMITTED,
        AttemptState.UNKNOWN,
        AttemptState.ACCEPTED,
        AttemptState.PARTIAL,
    }
)
_RELEASE_STATES = frozenset(
    {AttemptState.REJECTED, AttemptState.RECONCILED_NOT_FOUND}
)
_RESERVATION_FIELDS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "plan_id",
        "action_id",
        "account_id",
        "account_context_id",
        "account_identity_id",
        "customer_order_ref",
        "currency_code",
        "reserved_amount",
        "action_sha256",
        "funds_precheck_id",
        "funds_evidence_sha256",
        "ledger_path",
        "ledger_state",
        "ledger_snapshot_sha256",
        "ledger_event_count",
        "status",
    }
)

# Capture canonical ledger readers once. Authority-bearing action/state
# projection is derived from one integrity-verified byte snapshot.
_LEDGER_SNAPSHOT = RealExecutionLedger.verified_snapshot
_LEDGER_PARSE = RealExecutionLedger._parse
_LEDGER_ACTION = RealExecutionLedger._action_payload
_LEDGER_STATE = RealExecutionLedger._state


class BetfairPreTradeReservationError(RuntimeError):
    """Exposure cannot be safely admitted or reconstructed."""


class ReservationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"


@dataclass(frozen=True, slots=True)
class BetfairExposureReservation:
    attempt_id: str
    plan_id: str
    action_id: str
    account_id: str
    account_context_id: str
    account_identity_id: str
    customer_order_ref: str | None
    currency_code: str
    reserved_amount: Decimal
    action_sha256: str
    funds_precheck_id: str
    funds_evidence_sha256: str
    ledger_path: str
    ledger_state: AttemptState
    ledger_snapshot_sha256: str
    ledger_event_count: int
    status: ReservationStatus

    @property
    def active(self) -> bool:
        return self.status is ReservationStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class _LedgerAttempt:
    attempt_id: str
    plan_id: str
    ordinal: int
    action: ExecutionAction
    action_sha256: str
    state: AttemptState


@dataclass(frozen=True, slots=True)
class _LedgerView:
    path: str
    snapshot_sha256: str
    event_count: int
    attempts: tuple[_LedgerAttempt, ...]

    def require(self, *, plan_id: str, attempt_id: str) -> _LedgerAttempt:
        matches = [
            item for item in self.attempts if item.attempt_id == attempt_id
        ]
        if len(matches) != 1:
            raise BetfairPreTradeReservationError(
                "attempt is not uniquely present in execution ledger"
            )
        item = matches[0]
        if item.plan_id != plan_id:
            raise BetfairPreTradeReservationError(
                "attempt belongs to a different execution plan"
            )
        return item


def worst_case_incremental_exposure(action: ExecutionAction) -> Decimal:
    """Conservative ordinary Betfair LIMIT risk: BACK stake, LAY liability."""

    if type(action) is not ExecutionAction:
        raise BetfairPreTradeReservationError(
            "action must be exact ExecutionAction"
        )
    if action.bookmaker_id != _VENUE_ID:
        raise BetfairPreTradeReservationError("action is not Betfair")
    stake = _positive_decimal(action.requested_stake, "requested_stake")
    odds = _positive_decimal(action.requested_odds, "requested_odds")
    if action.side == "BACK":
        return stake
    if action.side == "LAY":
        if odds <= Decimal("1"):
            raise BetfairPreTradeReservationError(
                "LAY requested_odds must exceed 1"
            )
        odds_digits = max(len(odds.as_tuple().digits), odds.adjusted() + 1)
        precision = len(stake.as_tuple().digits) + odds_digits + 4
        if precision > 10000:
            raise BetfairPreTradeReservationError(
                "LAY exposure arithmetic exceeds bounded exact precision"
            )
        with localcontext() as context:
            context.prec = max(50, precision)
            liability = stake * (odds - Decimal("1"))
        return _bounded_decimal_shape(
            liability,
            "worst_case_incremental_exposure",
        )
    raise BetfairPreTradeReservationError(
        "only BACK/LAY exposure is supported"
    )


class BetfairPreTradeReservationStore:
    """SQLite-backed account-level exposure reservations.

    BEGIN IMMEDIATE serializes competing admissions across processes. The
    execution ledger is also an independent completeness oracle: before
    admitting new exposure, every earlier unresolved Betfair attempt for this
    account must already have an ACTIVE reservation. Deleting or restoring an
    older reservation database therefore cannot silently free capital while
    canonical execution truth still records unresolved external effect.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        account_id: str,
        currency_code: str,
    ) -> None:
        self.path = Path(path)
        self.account_id = _text(account_id, "account_id")
        self.currency_code = _currency(currency_code)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def reserve(
        self,
        *,
        plan_id: str,
        attempt_id: str,
        funds_precheck: BetfairAccountFundsPrecheck,
        execution_ledger: RealExecutionLedger,
        customer_order_ref: str | None = None,
    ) -> BetfairExposureReservation:
        """Atomically reserve exposure for one exact RESERVED ledger attempt."""

        plan_id = _text(plan_id, "plan_id")
        attempt_id = _text(attempt_id, "attempt_id")
        customer_order_ref = (
            None
            if customer_order_ref is None
            else _text(customer_order_ref, "customer_order_ref")
        )

        ledger_view = _ledger_view(execution_ledger)
        attempt = ledger_view.require(plan_id=plan_id, attempt_id=attempt_id)
        action = attempt.action
        if attempt.state is not AttemptState.RESERVED:
            raise BetfairPreTradeReservationError(
                "new reservation requires RESERVED execution attempt"
            )
        if action.bookmaker_id != _VENUE_ID or action.account_id != self.account_id:
            raise BetfairPreTradeReservationError(
                "execution action is outside this Betfair account store"
            )
        required = worst_case_incremental_exposure(action)

        try:
            precheck = require_authoritative_funds_precheck(funds_precheck)
        except (BetfairAccountFundsPrecheckError, TypeError, ValueError) as exc:
            raise BetfairPreTradeReservationError(
                "funds precheck lacks current product authority"
            ) from exc
        if precheck.currency_code != self.currency_code:
            raise BetfairPreTradeReservationError(
                "funds precheck currency mismatch"
            )
        if precheck.required_liability != required:
            raise BetfairPreTradeReservationError(
                "funds precheck liability does not match durable action"
            )
        if not precheck.passed:
            raise BetfairPreTradeReservationError(
                "provider funds are insufficient"
            )

        candidate = BetfairExposureReservation(
            attempt_id=attempt_id,
            plan_id=plan_id,
            action_id=action.action_id,
            account_id=self.account_id,
            account_context_id=_context_id(precheck.account_context_id),
            account_identity_id=_sha(
                precheck.account_identity_id, "account_identity_id"
            ),
            customer_order_ref=customer_order_ref,
            currency_code=self.currency_code,
            reserved_amount=required,
            action_sha256=attempt.action_sha256,
            funds_precheck_id=_sha(precheck.precheck_id, "funds_precheck_id"),
            funds_evidence_sha256=_sha(
                precheck.account_funds_sha256,
                "funds_evidence_sha256",
            ),
            ledger_path=ledger_view.path,
            ledger_state=attempt.state,
            ledger_snapshot_sha256=ledger_view.snapshot_sha256,
            ledger_event_count=ledger_view.event_count,
            status=ReservationStatus.ACTIVE,
        )

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                locked_precheck = require_authoritative_funds_precheck(
                    funds_precheck
                )
            except (BetfairAccountFundsPrecheckError, TypeError, ValueError) as exc:
                raise BetfairPreTradeReservationError(
                    "funds precheck expired while waiting for reservation lock"
                ) from exc
            if (
                locked_precheck is not precheck
                or locked_precheck.precheck_id != candidate.funds_precheck_id
            ):
                raise BetfairPreTradeReservationError(
                    "funds precheck identity changed before atomic admission"
                )
            self._bind_ledger_path(conn, ledger_view.path)
            reservations = self._read_all(conn)
            existing = reservations.get(attempt_id)
            if existing is not None:
                if _immutable_key(existing) != _immutable_key(candidate):
                    raise BetfairPreTradeReservationError(
                        "attempt_id already owns a different reservation"
                    )
                conn.commit()
                return existing

            self._require_ledger_completeness(
                ledger_view=ledger_view,
                reservations=reservations,
                candidate=attempt,
            )
            active = [item for item in reservations.values() if item.active]
            if any(
                item.account_context_id != precheck.account_context_id
                for item in active
            ):
                raise BetfairPreTradeReservationError(
                    "active capital belongs to a different authenticated account context"
                )
            committed = _exact_decimal_sum(
                [item.reserved_amount for item in active] + [required]
            )
            if precheck.available_to_bet_balance < committed:
                raise BetfairPreTradeReservationError(
                    "provider balance minus local reservations is insufficient"
                )
            self._insert(conn, candidate)
            conn.commit()
            return candidate
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def sync_from_ledger(
        self,
        *,
        attempt_id: str,
        execution_ledger: RealExecutionLedger,
    ) -> BetfairExposureReservation:
        """Update reservation state from one exact canonical ledger snapshot."""

        attempt_id = _text(attempt_id, "attempt_id")
        ledger_view = _ledger_view(execution_ledger)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            reservations = self._read_all(conn)
            current = reservations.get(attempt_id)
            if current is None:
                raise KeyError(attempt_id)
            attempt = ledger_view.require(
                plan_id=current.plan_id,
                attempt_id=attempt_id,
            )
            if (
                attempt.action.action_id != current.action_id
                or attempt.action_sha256 != current.action_sha256
                or ledger_view.path != current.ledger_path
            ):
                raise BetfairPreTradeReservationError(
                    "execution authority no longer matches reservation identity"
                )
            _require_nonrollback(current.ledger_state, attempt.state)
            status = (
                ReservationStatus.RELEASED
                if attempt.state in _RELEASE_STATES
                else ReservationStatus.ACTIVE
            )
            updated = BetfairExposureReservation(
                attempt_id=current.attempt_id,
                plan_id=current.plan_id,
                action_id=current.action_id,
                account_id=current.account_id,
                account_context_id=current.account_context_id,
                account_identity_id=current.account_identity_id,
                customer_order_ref=current.customer_order_ref,
                currency_code=current.currency_code,
                reserved_amount=current.reserved_amount,
                action_sha256=current.action_sha256,
                funds_precheck_id=current.funds_precheck_id,
                funds_evidence_sha256=current.funds_evidence_sha256,
                ledger_path=current.ledger_path,
                ledger_state=attempt.state,
                ledger_snapshot_sha256=ledger_view.snapshot_sha256,
                ledger_event_count=ledger_view.event_count,
                status=status,
            )
            self._replace(conn, updated)
            conn.commit()
            return updated
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, attempt_id: str) -> BetfairExposureReservation:
        attempt_id = _text(attempt_id, "attempt_id")
        conn = self._connect()
        try:
            rows = self._read_all(conn)
            if attempt_id not in rows:
                raise KeyError(attempt_id)
            return rows[attempt_id]
        finally:
            conn.close()

    def active_reserved_amount(self) -> Decimal:
        conn = self._connect()
        try:
            return _exact_decimal_sum(
                item.reserved_amount
                for item in self._read_all(conn).values()
                if item.active
            )
        finally:
            conn.close()

    def _require_ledger_completeness(
        self,
        *,
        ledger_view: _LedgerView,
        reservations: dict[str, BetfairExposureReservation],
        candidate: _LedgerAttempt,
    ) -> None:
        for attempt in ledger_view.attempts:
            if (
                attempt.attempt_id == candidate.attempt_id
                or attempt.action.bookmaker_id != _VENUE_ID
                or attempt.action.account_id != self.account_id
                or attempt.state not in _ACTIVE_STATES
            ):
                continue
            # Later pure RESERVED attempts may be queued behind the candidate and
            # have not reached local-capital admission yet. Any later attempt
            # that has already advanced past RESERVED must already own capital.
            if (
                attempt.ordinal >= candidate.ordinal
                and attempt.state is AttemptState.RESERVED
            ):
                continue
            reservation = reservations.get(attempt.attempt_id)
            if reservation is None or not reservation.active:
                raise BetfairPreTradeReservationError(
                    "unresolved ledger attempt lacks active local reservation"
                )
            if reservation.action_sha256 != attempt.action_sha256:
                raise BetfairPreTradeReservationError(
                    "local reservation disagrees with unresolved ledger action"
                )

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reservation_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    schema_version INTEGER NOT NULL,
                    venue_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    currency_code TEXT NOT NULL,
                    ledger_path TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reservations (
                    attempt_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL
                )
                """
            )
            row = conn.execute(
                "SELECT schema_version, venue_id, account_id, currency_code "
                "FROM reservation_meta WHERE singleton = 1"
            ).fetchone()
            expected = (
                _SCHEMA_VERSION,
                _VENUE_ID,
                self.account_id,
                self.currency_code,
            )
            if row is None:
                conn.execute(
                    "INSERT INTO reservation_meta "
                    "(singleton, schema_version, venue_id, account_id, currency_code) "
                    "VALUES (1, ?, ?, ?, ?)",
                    expected,
                )
            elif tuple(row) != expected:
                raise BetfairPreTradeReservationError(
                    "reservation store identity mismatch"
                )
            self._read_all(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _bind_ledger_path(
        self,
        conn: sqlite3.Connection,
        ledger_path: str,
    ) -> None:
        ledger_path = _text(ledger_path, "ledger_path")
        row = conn.execute(
            "SELECT ledger_path FROM reservation_meta WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise BetfairPreTradeReservationError(
                "reservation metadata is missing"
            )
        if row[0] is None:
            conn.execute(
                "UPDATE reservation_meta SET ledger_path = ? WHERE singleton = 1",
                (ledger_path,),
            )
            return
        if row[0] != ledger_path:
            raise BetfairPreTradeReservationError(
                "reservation store is bound to a different execution ledger"
            )

    def _read_all(
        self, conn: sqlite3.Connection
    ) -> dict[str, BetfairExposureReservation]:
        result: dict[str, BetfairExposureReservation] = {}
        rows = conn.execute(
            "SELECT attempt_id, payload_json, payload_sha256 FROM reservations"
        ).fetchall()
        for attempt_id, raw, digest in rows:
            payload = _decode_payload(raw)
            if sha256(raw.encode("utf-8")).hexdigest() != _sha(
                digest, "payload_sha256"
            ):
                raise BetfairPreTradeReservationError(
                    "reservation payload hash mismatch"
                )
            item = _reservation_from_payload(payload)
            if (
                item.account_id != self.account_id
                or item.currency_code != self.currency_code
            ):
                raise BetfairPreTradeReservationError(
                    "reservation row is outside store identity"
                )
            if item.attempt_id != attempt_id or item.attempt_id in result:
                raise BetfairPreTradeReservationError(
                    "reservation primary-key identity mismatch"
                )
            result[item.attempt_id] = item
        return result

    def _insert(
        self,
        conn: sqlite3.Connection,
        item: BetfairExposureReservation,
    ) -> None:
        raw = _canonical_json(_reservation_payload(item))
        try:
            conn.execute(
                "INSERT INTO reservations "
                "(attempt_id, payload_json, payload_sha256) VALUES (?, ?, ?)",
                (
                    item.attempt_id,
                    raw,
                    sha256(raw.encode("utf-8")).hexdigest(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise BetfairPreTradeReservationError(
                "attempt reservation raced with another writer"
            ) from exc

    def _replace(
        self,
        conn: sqlite3.Connection,
        item: BetfairExposureReservation,
    ) -> None:
        raw = _canonical_json(_reservation_payload(item))
        cursor = conn.execute(
            "UPDATE reservations SET payload_json = ?, payload_sha256 = ? "
            "WHERE attempt_id = ?",
            (
                raw,
                sha256(raw.encode("utf-8")).hexdigest(),
                item.attempt_id,
            ),
        )
        if cursor.rowcount != 1:
            raise BetfairPreTradeReservationError(
                "reservation disappeared during update"
            )


def _ledger_view(ledger: RealExecutionLedger) -> _LedgerView:
    if type(ledger) is not RealExecutionLedger:
        raise BetfairPreTradeReservationError(
            "execution_ledger must be exact RealExecutionLedger"
        )
    try:
        snapshot = _LEDGER_SNAPSHOT(ledger)
        events = _LEDGER_PARSE(snapshot.payload)
        attempts: list[_LedgerAttempt] = []
        seen: set[str] = set()
        for ordinal, event in enumerate(events):
            if event.get("event_type") != EventType.ATTEMPT_RESERVED.value:
                continue
            attempt_id = _text(event.get("attempt_id"), "attempt_id")
            if attempt_id in seen:
                raise BetfairPreTradeReservationError(
                    "execution ledger repeats attempt reservation"
                )
            seen.add(attempt_id)
            plan_id = _text(event.get("plan_id"), "plan_id")
            action_id = _text(event.get("action_id"), "action_id")
            related = [
                candidate
                for candidate in events
                if candidate.get("attempt_id") == attempt_id
            ]
            state = _LEDGER_STATE(related)
            if state is None:
                raise BetfairPreTradeReservationError(
                    "reserved attempt has no ledger state"
                )
            _, raw_action = _LEDGER_ACTION(events, plan_id, action_id)
            action = _action_from_dict(raw_action)
            attempts.append(
                _LedgerAttempt(
                    attempt_id=attempt_id,
                    plan_id=plan_id,
                    ordinal=ordinal,
                    action=action,
                    action_sha256=_digest(action.to_dict()),
                    state=state,
                )
            )
    except BetfairPreTradeReservationError:
        raise
    except (ExecutionLedgerError, KeyError, TypeError, ValueError) as exc:
        raise BetfairPreTradeReservationError(
            "execution ledger cannot supply canonical reservation authority"
        ) from exc
    return _LedgerView(
        path=str(ledger.path.resolve()),
        snapshot_sha256=_sha(snapshot.sha256, "ledger_snapshot_sha256"),
        event_count=_nonnegative_int(snapshot.event_count, "ledger_event_count"),
        attempts=tuple(attempts),
    )


def _action_from_dict(raw: object) -> ExecutionAction:
    if type(raw) is not dict:
        raise BetfairPreTradeReservationError("stored action must be object")
    required = {
        "action_id",
        "bookmaker_id",
        "account_id",
        "event_id",
        "market_id",
        "selection_id",
        "side",
        "requested_odds",
        "requested_stake",
        "quote_id",
        "quote_observed_at",
        "expires_at",
    }
    if set(raw) != required:
        raise BetfairPreTradeReservationError(
            "stored action has unexpected fields"
        )
    try:
        return ExecutionAction(**raw)
    except (TypeError, ValueError) as exc:
        raise BetfairPreTradeReservationError(
            "stored action is invalid"
        ) from exc


def _reservation_payload(item: BetfairExposureReservation) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "attempt_id": item.attempt_id,
        "plan_id": item.plan_id,
        "action_id": item.action_id,
        "account_id": item.account_id,
        "account_context_id": item.account_context_id,
        "account_identity_id": item.account_identity_id,
        "customer_order_ref": item.customer_order_ref,
        "currency_code": item.currency_code,
        "reserved_amount": _decimal_text(item.reserved_amount),
        "action_sha256": item.action_sha256,
        "funds_precheck_id": item.funds_precheck_id,
        "funds_evidence_sha256": item.funds_evidence_sha256,
        "ledger_path": item.ledger_path,
        "ledger_state": item.ledger_state.value,
        "ledger_snapshot_sha256": item.ledger_snapshot_sha256,
        "ledger_event_count": item.ledger_event_count,
        "status": item.status.value,
    }


def _reservation_from_payload(
    raw: dict[str, Any],
) -> BetfairExposureReservation:
    if (
        set(raw) != _RESERVATION_FIELDS
        or raw.get("schema_version") != _SCHEMA_VERSION
    ):
        raise BetfairPreTradeReservationError(
            "reservation payload schema mismatch"
        )
    state = _attempt_state(raw["ledger_state"])
    status = _reservation_status(raw["status"])
    if (state in _RELEASE_STATES) != (status is ReservationStatus.RELEASED):
        raise BetfairPreTradeReservationError(
            "reservation status contradicts ledger state"
        )
    item = BetfairExposureReservation(
        attempt_id=_text(raw["attempt_id"], "attempt_id"),
        plan_id=_text(raw["plan_id"], "plan_id"),
        action_id=_text(raw["action_id"], "action_id"),
        account_id=_text(raw["account_id"], "account_id"),
        account_context_id=_context_id(raw["account_context_id"]),
        account_identity_id=_sha(
            raw["account_identity_id"], "account_identity_id"
        ),
        customer_order_ref=(
            None
            if raw["customer_order_ref"] is None
            else _text(raw["customer_order_ref"], "customer_order_ref")
        ),
        currency_code=_currency(raw["currency_code"]),
        reserved_amount=_decimal_from_text(
            raw["reserved_amount"], "reserved_amount"
        ),
        action_sha256=_sha(raw["action_sha256"], "action_sha256"),
        funds_precheck_id=_sha(
            raw["funds_precheck_id"], "funds_precheck_id"
        ),
        funds_evidence_sha256=_sha(
            raw["funds_evidence_sha256"], "funds_evidence_sha256"
        ),
        ledger_path=_text(raw["ledger_path"], "ledger_path"),
        ledger_state=state,
        ledger_snapshot_sha256=_sha(
            raw["ledger_snapshot_sha256"], "ledger_snapshot_sha256"
        ),
        ledger_event_count=_nonnegative_int(
            raw["ledger_event_count"], "ledger_event_count"
        ),
        status=status,
    )
    if item.reserved_amount <= 0:
        raise BetfairPreTradeReservationError(
            "reserved_amount must be positive"
        )
    return item


def _immutable_key(item: BetfairExposureReservation) -> tuple[object, ...]:
    return (
        item.attempt_id,
        item.plan_id,
        item.action_id,
        item.account_id,
        item.account_context_id,
        item.customer_order_ref,
        item.currency_code,
        item.reserved_amount,
        item.action_sha256,
        item.ledger_path,
    )


def _require_nonrollback(previous: AttemptState, current: AttemptState) -> None:
    allowed = {
        AttemptState.RESERVED: _ACTIVE_STATES | _RELEASE_STATES,
        AttemptState.SUBMITTED: {
            AttemptState.SUBMITTED,
            AttemptState.UNKNOWN,
            AttemptState.ACCEPTED,
            AttemptState.PARTIAL,
            AttemptState.REJECTED,
            AttemptState.RECONCILED_NOT_FOUND,
        },
        AttemptState.UNKNOWN: {
            AttemptState.UNKNOWN,
            AttemptState.ACCEPTED,
            AttemptState.PARTIAL,
            AttemptState.REJECTED,
            AttemptState.RECONCILED_NOT_FOUND,
        },
        AttemptState.ACCEPTED: {AttemptState.ACCEPTED},
        AttemptState.PARTIAL: {AttemptState.PARTIAL},
        AttemptState.REJECTED: {AttemptState.REJECTED},
        AttemptState.RECONCILED_NOT_FOUND: {
            AttemptState.RECONCILED_NOT_FOUND
        },
    }
    if current not in allowed.get(previous, set()):
        raise BetfairPreTradeReservationError(
            "execution ledger state would roll back reservation history"
        )


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairPreTradeReservationError(
            "reservation payload is not canonical JSON"
        ) from exc


def _decode_payload(raw: object) -> dict[str, Any]:
    if type(raw) is not str:
        raise BetfairPreTradeReservationError(
            "reservation payload must be text"
        )

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise BetfairPreTradeReservationError(
                    "reservation payload contains duplicate JSON key"
                )
            result[key] = value
        return result

    def nonfinite(value: str) -> object:
        raise BetfairPreTradeReservationError(
            "reservation payload contains non-finite JSON"
        )

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=nonfinite,
        )
    except json.JSONDecodeError as exc:
        raise BetfairPreTradeReservationError(
            "reservation payload is invalid JSON"
        ) from exc
    if type(value) is not dict or _canonical_json(value) != raw:
        raise BetfairPreTradeReservationError(
            "reservation payload is not canonical JSON"
        )
    return value


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise BetfairPreTradeReservationError(
            f"{field} must be canonical non-empty text"
        )
    return value


def _sha(value: object, field: str) -> str:
    raw = _text(value, field)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise BetfairPreTradeReservationError(
            f"{field} must be lowercase SHA-256"
        )
    return raw


def _context_id(value: object) -> str:
    raw = _text(value, "account_context_id")
    prefix = "betfair-session-context:"
    if not raw.startswith(prefix):
        raise BetfairPreTradeReservationError(
            "account_context_id is not K07 Betfair context"
        )
    _sha(raw[len(prefix):], "account_context_id digest")
    return raw


def _currency(value: object) -> str:
    raw = _text(value, "currency_code")
    if (
        len(raw) != 3
        or not raw.isascii()
        or not raw.isalpha()
        or raw != raw.upper()
    ):
        raise BetfairPreTradeReservationError(
            "currency_code must be three uppercase ASCII letters"
        )
    return raw


def _exact_decimal_sum(values) -> Decimal:
    items = tuple(values)
    if not items:
        return _ZERO
    if any(type(value) is not Decimal or not value.is_finite() for value in items):
        raise BetfairPreTradeReservationError(
            "reservation sum requires finite exact Decimals"
        )
    minimum_exponent = min(value.as_tuple().exponent for value in items)
    total = 0
    for value in items:
        sign, digits, exponent = value.as_tuple()
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        if sign:
            coefficient = -coefficient
        total += coefficient * (10 ** (exponent - minimum_exponent))
    if total == 0:
        return Decimal("0")
    sign = 1 if total < 0 else 0
    digits = tuple(int(ch) for ch in str(abs(total)))
    return Decimal((sign, digits, minimum_exponent))


def _bounded_decimal_shape(value: Decimal, field: str) -> Decimal:
    sign, digits, exponent = value.as_tuple()
    del sign
    if (
        len(digits) > 1000
        or not isinstance(exponent, int)
        or abs(exponent) > 1000
        or abs(value.adjusted()) > 1000
    ):
        raise BetfairPreTradeReservationError(
            f"{field} exceeds bounded exact Decimal shape"
        )
    return value


def _positive_decimal(value: object, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise BetfairPreTradeReservationError(
            f"{field} must be positive finite exact Decimal"
        )
    return _bounded_decimal_shape(value, field)


def _decimal_text(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise BetfairPreTradeReservationError(
            "money must be finite exact Decimal"
        )
    return format(value, "f")


def _decimal_from_text(value: object, field: str) -> Decimal:
    raw = _text(value, field)
    try:
        parsed = Decimal(raw)
    except Exception as exc:
        raise BetfairPreTradeReservationError(
            f"{field} must be Decimal text"
        ) from exc
    if not parsed.is_finite() or format(parsed, "f") != raw:
        raise BetfairPreTradeReservationError(
            f"{field} must be canonical finite Decimal text"
        )
    return _bounded_decimal_shape(parsed, field)


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise BetfairPreTradeReservationError(
            f"{field} must be non-negative integer"
        )
    return value


def _attempt_state(value: object) -> AttemptState:
    if type(value) is not str:
        raise BetfairPreTradeReservationError("ledger_state must be text")
    try:
        return AttemptState(value)
    except ValueError as exc:
        raise BetfairPreTradeReservationError(
            "unsupported ledger_state"
        ) from exc


def _reservation_status(value: object) -> ReservationStatus:
    if type(value) is not str:
        raise BetfairPreTradeReservationError(
            "reservation status must be text"
        )
    try:
        return ReservationStatus(value)
    except ValueError as exc:
        raise BetfairPreTradeReservationError(
            "unsupported reservation status"
        ) from exc
