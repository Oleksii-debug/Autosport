"""Read-only reconciliation for canonical bookmaker account snapshots.

This is diagnostic evidence only. It never grants execution, settlement, bankroll,
account-identity, provider-write, or real-money authority, and an available-balance
delta is never interpreted as P&L.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, DecimalException, localcontext
from enum import Enum
from hashlib import sha256
import json

from .bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerCapability,
    BookmakerPositionObservation,
)


class BookmakerAccountReconciliationError(ValueError):
    pass


class AccountReconciliationStatus(str, Enum):
    RECONCILED_DIAGNOSTIC = "reconciled_diagnostic"
    UNRESOLVED_POSITION_GAP = "unresolved_position_gap"
    INCOMPLETE_OBSERVATION = "incomplete_observation"


class AccountPositionTransition(str, Enum):
    OPEN_RETAINED = "open_retained"
    OPEN_TO_SETTLED = "open_to_settled"
    OPEN_DISAPPEARED_UNRESOLVED = "open_disappeared_unresolved"
    NEW_OPEN = "new_open"
    SETTLED_RETAINED = "settled_retained"
    SETTLED_OBSERVED_WITHOUT_PRIOR_OPEN = "settled_observed_without_prior_open"


_REQUIRED = frozenset(
    {
        BookmakerCapability.BALANCE_READ,
        BookmakerCapability.OPEN_POSITIONS_READ,
        BookmakerCapability.SETTLED_POSITIONS_READ,
    }
)


@dataclass(frozen=True, slots=True)
class AccountPositionReconciliation:
    external_position_id: str
    transition: AccountPositionTransition
    previous_observation_id: str | None
    current_observation_id: str | None


@dataclass(frozen=True, slots=True)
class BookmakerAccountReconciliationReport:
    venue_id: str
    account_id: str
    adapter_id: str
    currency: str | None
    previous_snapshot_at: str
    current_snapshot_at: str
    previous_profile_id: str
    current_profile_id: str
    previous_profile_version: int
    current_profile_version: int
    missing_capabilities: tuple[str, ...]
    available_balance_delta: Decimal | None
    position_transitions: tuple[AccountPositionReconciliation, ...]
    unresolved_external_position_ids: tuple[str, ...]
    status: AccountReconciliationStatus
    evidence_id: str

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def settlement_authorized(self) -> bool:
        return False

    @property
    def balance_delta_is_pnl(self) -> bool:
        return False


def _timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise BookmakerAccountReconciliationError(
            "snapshot timestamp must be ISO-8601"
        ) from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise BookmakerAccountReconciliationError(
            "snapshot timestamp must include timezone"
        )
    return result


def _currency(snapshot: BookmakerAccountSnapshot) -> str | None:
    values: set[str] = set()
    if snapshot.balance is not None:
        values.add(snapshot.balance.currency)
    values.update(item.currency for item in snapshot.open_positions)
    values.update(item.currency for item in snapshot.settled_positions)
    if len(values) > 1:
        raise BookmakerAccountReconciliationError(
            "one snapshot cannot mix currencies"
        )
    return next(iter(values), None)


def _balance_delta(current: Decimal, previous: Decimal) -> Decimal:
    current_tuple, previous_tuple = current.as_tuple(), previous.as_tuple()
    precision = max(
        34,
        len(current_tuple.digits)
        + len(previous_tuple.digits)
        + abs(current_tuple.exponent - previous_tuple.exponent)
        + 4,
    )
    try:
        with localcontext(
            Context(prec=precision, Emin=-999999999, Emax=999999999)
        ):
            result = current - previous
    except DecimalException as exc:
        raise BookmakerAccountReconciliationError(
            "balance delta is not representable"
        ) from exc
    if not result.is_finite():
        raise BookmakerAccountReconciliationError(
            "balance delta must be finite"
        )
    return result


def _profile_semantics(snapshot: BookmakerAccountSnapshot) -> tuple[object, ...]:
    profile = snapshot.profile
    return (
        profile.adapter_version,
        tuple(
            sorted(
                (fact.capability.value, fact.state.value)
                for fact in profile.facts
            )
        ),
    )


def _position_payload(
    item: BookmakerPositionObservation,
) -> dict[str, object]:
    return {
        "id": item.external_position_id,
        "observation_id": item.observation_id,
        "state": item.state.value,
        "currency": item.currency,
        "observed_at": item.observed_at,
        "source_sha256": item.source_payload_sha256,
        "amount": None if item.provider_amount is None else str(item.provider_amount),
        "amount_semantics": item.provider_amount_semantics,
        "side": item.provider_side,
        "odds": None if item.decimal_odds is None else str(item.decimal_odds),
        "gross_return": (
            None if item.gross_return is None else str(item.gross_return)
        ),
        "receipt": item.external_receipt_id,
    }


def _snapshot_payload(
    snapshot: BookmakerAccountSnapshot,
) -> dict[str, object]:
    balance = None
    if snapshot.balance is not None:
        item = snapshot.balance
        balance = {
            "observation_id": item.observation_id,
            "currency": item.currency,
            "available": str(item.available_balance),
            "total": (
                None if item.total_balance is None else str(item.total_balance)
            ),
            "exposure": (
                None if item.exposure is None else str(item.exposure)
            ),
            "retained_commission": (
                None
                if item.retained_commission is None
                else str(item.retained_commission)
            ),
            "exposure_limit": (
                None
                if item.exposure_limit is None
                else str(item.exposure_limit)
            ),
            "observed_at": item.observed_at,
            "source_sha256": item.source_payload_sha256,
        }
    sort_key = lambda item: (
        item.external_position_id,
        item.observation_id,
    )
    return {
        "profile_id": snapshot.profile.profile_id,
        "observed_at": snapshot.observed_at,
        "capabilities": sorted(
            item.value for item in snapshot.observed_capabilities
        ),
        "balance": balance,
        "open": [
            _position_payload(item)
            for item in sorted(snapshot.open_positions, key=sort_key)
        ],
        "settled": [
            _position_payload(item)
            for item in sorted(snapshot.settled_positions, key=sort_key)
        ],
    }


def _digest(
    previous,
    current,
    status,
    missing,
    delta,
    rows,
    unresolved,
) -> str:
    payload = {
        "schema": "bookmaker-account-readonly-reconciliation-v1",
        "previous": _snapshot_payload(previous),
        "current": _snapshot_payload(current),
        "status": status.value,
        "missing": list(missing),
        "balance_delta": None if delta is None else str(delta),
        "rows": [
            [
                row.external_position_id,
                row.transition.value,
                row.previous_observation_id,
                row.current_observation_id,
            ]
            for row in rows
        ],
        "unresolved": list(unresolved),
    }
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def reconcile_bookmaker_account_snapshots(
    previous: BookmakerAccountSnapshot,
    current: BookmakerAccountSnapshot,
) -> BookmakerAccountReconciliationReport:
    """Compare two validated snapshots without upgrading them into execution truth."""
    if (
        type(previous) is not BookmakerAccountSnapshot
        or type(current) is not BookmakerAccountSnapshot
    ):
        raise TypeError(
            "previous and current must be exact BookmakerAccountSnapshot values"
        )

    p, c = previous.profile, current.profile
    if (
        p.venue_id,
        p.account_id,
        p.adapter_id,
    ) != (
        c.venue_id,
        c.account_id,
        c.adapter_id,
    ):
        raise BookmakerAccountReconciliationError(
            "snapshot venue/account/adapter identity mismatch"
        )
    if _timestamp(current.observed_at) <= _timestamp(previous.observed_at):
        raise BookmakerAccountReconciliationError(
            "current snapshot must be strictly later than previous snapshot"
        )
    if c.profile_version < p.profile_version:
        raise BookmakerAccountReconciliationError(
            "capability profile version rolled back"
        )
    if (
        c.profile_version == p.profile_version
        and _profile_semantics(current) != _profile_semantics(previous)
    ):
        raise BookmakerAccountReconciliationError(
            "same profile version changed capability semantics"
        )

    previous_currency = _currency(previous)
    current_currency = _currency(current)
    if (
        previous_currency
        and current_currency
        and previous_currency != current_currency
    ):
        raise BookmakerAccountReconciliationError(
            "snapshot currency changed"
        )
    currency = current_currency or previous_currency

    missing = tuple(
        sorted(
            cap.value
            for cap in _REQUIRED
            if cap not in previous.observed_capabilities
            or cap not in current.observed_capabilities
        )
    )
    delta = None
    if previous.balance is not None and current.balance is not None:
        delta = _balance_delta(
            current.balance.available_balance,
            previous.balance.available_balance,
        )

    rows: tuple[AccountPositionReconciliation, ...] = ()
    unresolved: tuple[str, ...] = ()
    if missing:
        status = AccountReconciliationStatus.INCOMPLETE_OBSERVATION
    else:
        po = {
            x.external_position_id: x
            for x in previous.open_positions
        }
        ps = {
            x.external_position_id: x
            for x in previous.settled_positions
        }
        co = {
            x.external_position_id: x
            for x in current.open_positions
        }
        cs = {
            x.external_position_id: x
            for x in current.settled_positions
        }
        reopened = sorted(ps.keys() & co.keys())
        if reopened:
            raise BookmakerAccountReconciliationError(
                f"settled position reappeared open: {reopened[0]}"
            )

        built: list[AccountPositionReconciliation] = []
        gaps: list[str] = []
        for position_id in sorted(
            set(po) | set(ps) | set(co) | set(cs)
        ):
            if position_id in po:
                if position_id in co:
                    transition = AccountPositionTransition.OPEN_RETAINED
                    current_id = co[position_id].observation_id
                elif position_id in cs:
                    transition = AccountPositionTransition.OPEN_TO_SETTLED
                    current_id = cs[position_id].observation_id
                else:
                    transition = (
                        AccountPositionTransition.OPEN_DISAPPEARED_UNRESOLVED
                    )
                    current_id = None
                    gaps.append(position_id)
                built.append(
                    AccountPositionReconciliation(
                        position_id,
                        transition,
                        po[position_id].observation_id,
                        current_id,
                    )
                )
            elif position_id in co:
                built.append(
                    AccountPositionReconciliation(
                        position_id,
                        AccountPositionTransition.NEW_OPEN,
                        None,
                        co[position_id].observation_id,
                    )
                )
            elif position_id in cs:
                transition = (
                    AccountPositionTransition.SETTLED_RETAINED
                    if position_id in ps
                    else AccountPositionTransition.SETTLED_OBSERVED_WITHOUT_PRIOR_OPEN
                )
                previous_id = (
                    ps[position_id].observation_id
                    if position_id in ps
                    else None
                )
                built.append(
                    AccountPositionReconciliation(
                        position_id,
                        transition,
                        previous_id,
                        cs[position_id].observation_id,
                    )
                )
        rows, unresolved = tuple(built), tuple(gaps)
        status = (
            AccountReconciliationStatus.UNRESOLVED_POSITION_GAP
            if unresolved
            else AccountReconciliationStatus.RECONCILED_DIAGNOSTIC
        )

    evidence_id = _digest(
        previous,
        current,
        status,
        missing,
        delta,
        rows,
        unresolved,
    )
    return BookmakerAccountReconciliationReport(
        venue_id=p.venue_id,
        account_id=p.account_id,
        adapter_id=p.adapter_id,
        currency=currency,
        previous_snapshot_at=previous.observed_at,
        current_snapshot_at=current.observed_at,
        previous_profile_id=p.profile_id,
        current_profile_id=c.profile_id,
        previous_profile_version=p.profile_version,
        current_profile_version=c.profile_version,
        missing_capabilities=missing,
        available_balance_delta=delta,
        position_transitions=rows,
        unresolved_external_position_ids=unresolved,
        status=status,
        evidence_id=evidence_id,
    )
