from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TypeAlias

from .betfair_account_readonly import (
    ADAPTER_ID as BETFAIR_ADAPTER_ID,
    ADAPTER_VERSION as BETFAIR_ADAPTER_VERSION,
    BetfairClearedOrderObservation,
    BetfairClearedOrderPage,
    BetfairCurrentOrderObservation,
    BetfairCurrentOrderPage,
)
from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityProfile,
)
from .real_execution_ledger import AcknowledgementStatus, ExecutionAction


class ProviderEvidenceError(RuntimeError):
    """Raised when read-only provider observations do not prove an execution fact."""


def _time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ProviderEvidenceError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderEvidenceError(f"{name} must be timezone-aware")
    return parsed


def _digest(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProviderEvidenceError("provider evidence is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def _current_order_payload(order: BetfairCurrentOrderObservation) -> dict[str, object]:
    return {
        "bet_id": order.bet_id,
        "market_id": order.market_id,
        "selection_id": order.selection_id,
        "side": order.side,
        "status": order.status,
        "placed_date": order.placed_date,
        "price": None if order.price is None else str(order.price),
        "requested_size": (
            None if order.requested_size is None else str(order.requested_size)
        ),
        "average_price_matched": str(order.average_price_matched),
        "size_matched": str(order.size_matched),
        "size_remaining": str(order.size_remaining),
        "customer_order_ref": order.customer_order_ref,
        "customer_strategy_ref": order.customer_strategy_ref,
        "evidence": {
            "observed_at": order.evidence.observed_at,
            "source_payload_sha256": order.evidence.source_payload_sha256,
        },
    }


def _cleared_order_payload(order: BetfairClearedOrderObservation) -> dict[str, object]:
    return {
        "bet_id": order.bet_id,
        "market_id": order.market_id,
        "selection_id": order.selection_id,
        "side": order.side,
        "bet_status": order.bet_status,
        "placed_date": order.placed_date,
        "settled_date": order.settled_date,
        "price_requested": str(order.price_requested),
        "price_matched": str(order.price_matched),
        "size_settled": str(order.size_settled),
        "profit": str(order.profit),
        "customer_order_ref": order.customer_order_ref,
        "customer_strategy_ref": order.customer_strategy_ref,
        "evidence": {
            "observed_at": order.evidence.observed_at,
            "source_payload_sha256": order.evidence.source_payload_sha256,
        },
    }


def _complete_current_pages(
    pages: tuple[BetfairCurrentOrderPage, ...],
) -> tuple[tuple[BetfairCurrentOrderObservation, ...], str, str]:
    if not pages or any(not isinstance(page, BetfairCurrentOrderPage) for page in pages):
        raise ProviderEvidenceError("current-order evidence requires canonical pages")
    expected_offset = 0
    orders: list[BetfairCurrentOrderObservation] = []
    payload: list[dict[str, object]] = []
    latest_raw = pages[0].evidence.observed_at
    latest = _time(latest_raw, "current page observed_at")
    for index, page in enumerate(pages):
        if page.from_record != expected_offset:
            raise ProviderEvidenceError("current-order pages are not contiguous")
        if index < len(pages) - 1 and not page.more_available:
            raise ProviderEvidenceError("current-order pages continue after terminal page")
        if index == len(pages) - 1 and page.more_available:
            raise ProviderEvidenceError("current-order evidence is incomplete")
        if page.more_available and not page.orders:
            raise ProviderEvidenceError("current-order pagination cannot advance")
        expected_offset += len(page.orders)
        orders.extend(page.orders)
        page_time = _time(page.evidence.observed_at, "current page observed_at")
        if page_time > latest:
            latest = page_time
            latest_raw = page.evidence.observed_at
        payload.append(
            {
                "from_record": page.from_record,
                "record_count": page.record_count,
                "more_available": page.more_available,
                "evidence": {
                    "observed_at": page.evidence.observed_at,
                    "source_payload_sha256": page.evidence.source_payload_sha256,
                },
                "orders": [_current_order_payload(order) for order in page.orders],
            }
        )
    if len({order.bet_id for order in orders}) != len(orders):
        raise ProviderEvidenceError("current-order evidence has duplicate bet_id")
    return tuple(orders), _digest(payload), latest_raw


def _complete_cleared_pages(
    pages: tuple[BetfairClearedOrderPage, ...],
) -> tuple[tuple[BetfairClearedOrderObservation, ...], str, str]:
    if not pages or any(not isinstance(page, BetfairClearedOrderPage) for page in pages):
        raise ProviderEvidenceError("cleared-order evidence requires canonical pages")
    expected_offset = 0
    orders: list[BetfairClearedOrderObservation] = []
    payload: list[dict[str, object]] = []
    latest_raw = pages[0].evidence.observed_at
    latest = _time(latest_raw, "cleared page observed_at")
    for index, page in enumerate(pages):
        if page.from_record != expected_offset:
            raise ProviderEvidenceError("cleared-order pages are not contiguous")
        if index < len(pages) - 1 and not page.more_available:
            raise ProviderEvidenceError("cleared-order pages continue after terminal page")
        if index == len(pages) - 1 and page.more_available:
            raise ProviderEvidenceError("cleared-order evidence is incomplete")
        if page.more_available and not page.orders:
            raise ProviderEvidenceError("cleared-order pagination cannot advance")
        expected_offset += len(page.orders)
        orders.extend(page.orders)
        page_time = _time(page.evidence.observed_at, "cleared page observed_at")
        if page_time > latest:
            latest = page_time
            latest_raw = page.evidence.observed_at
        payload.append(
            {
                "from_record": page.from_record,
                "record_count": page.record_count,
                "more_available": page.more_available,
                "evidence": {
                    "observed_at": page.evidence.observed_at,
                    "source_payload_sha256": page.evidence.source_payload_sha256,
                },
                "orders": [_cleared_order_payload(order) for order in page.orders],
            }
        )
    if len({order.bet_id for order in orders}) != len(orders):
        raise ProviderEvidenceError("cleared-order evidence has duplicate bet_id")
    return tuple(orders), _digest(payload), latest_raw


_SEAL = object()


@dataclass(frozen=True, slots=True)
class VerifiedProviderEffectEvidence:
    bookmaker_id: str
    account_id: str
    action_id: str
    adapter_id: str
    adapter_version: str
    profile_version: int
    event_id: str
    market_id: str
    selection_id: str
    external_receipt_id: str
    observed_at: str
    source_payload_sha256: str
    status: AcknowledgementStatus
    accepted_odds: Decimal
    accepted_stake: Decimal
    evidence_id: str
    _seal: object

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise ProviderEvidenceError(
                "verified provider effect evidence must come from canonical verifier"
            )


@dataclass(frozen=True, slots=True)
class VerifiedProviderAbsenceEvidence:
    bookmaker_id: str
    account_id: str
    action_id: str
    adapter_id: str
    adapter_version: str
    profile_version: int
    event_id: str
    market_id: str
    selection_id: str
    observed_at: str
    current_source_payload_sha256: str
    cleared_source_payload_sha256: str
    evidence_id: str
    _seal: object

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise ProviderEvidenceError(
                "verified provider absence evidence must come from canonical verifier"
            )


VerifiedProviderState: TypeAlias = (
    VerifiedProviderEffectEvidence | VerifiedProviderAbsenceEvidence
)


def _require_bound_profile(
    profile: BookmakerCapabilityProfile,
    *,
    expected_profile_sha256: str,
    bookmaker_id: str,
    account_id: str,
    observed_at: str,
) -> None:
    if not isinstance(profile, BookmakerCapabilityProfile):
        raise ProviderEvidenceError("provider evidence requires canonical capability profile")
    if (
        profile.venue_id != bookmaker_id
        or profile.account_id != account_id
        or profile.adapter_id != BETFAIR_ADAPTER_ID
        or profile.adapter_version != BETFAIR_ADAPTER_VERSION
        or profile.profile_id != expected_profile_sha256
    ):
        raise ProviderEvidenceError("provider capability profile is not exact bound profile")
    try:
        profile.require(BookmakerCapability.BET_READBACK)
    except Exception as exc:
        raise ProviderEvidenceError("BET_READBACK capability is not supported") from exc
    if _time(profile.observed_at, "profile observed_at") > _time(
        observed_at, "provider observed_at"
    ):
        raise ProviderEvidenceError("provider evidence predates capability profile")


def verify_betfair_provider_state(
    action: ExecutionAction,
    profile: BookmakerCapabilityProfile,
    *,
    expected_profile_sha256: str,
    current_pages: tuple[BetfairCurrentOrderPage, ...],
    cleared_pages: tuple[BetfairClearedOrderPage, ...],
) -> VerifiedProviderState:
    """Derive effect/absence only from complete canonical Betfair read-only pages.

    The future write adapter must submit ExecutionAction.action_id as Betfair
    customerOrderRef. This verifier never accepts caller-selected status, amount,
    receipt, or opaque evidence hashes as execution truth.
    """

    if not isinstance(action, ExecutionAction):
        raise ProviderEvidenceError("action must be canonical ExecutionAction")
    current, current_sha, current_at = _complete_current_pages(current_pages)
    cleared, cleared_sha, cleared_at = _complete_cleared_pages(cleared_pages)
    observed_at = (
        current_at
        if _time(current_at, "current observed_at")
        >= _time(cleared_at, "cleared observed_at")
        else cleared_at
    )
    _require_bound_profile(
        profile,
        expected_profile_sha256=expected_profile_sha256,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        observed_at=observed_at,
    )
    try:
        provider_selection_id = int(action.selection_id)
    except (TypeError, ValueError) as exc:
        raise ProviderEvidenceError(
            "Betfair readback requires provider-native numeric selection_id"
        ) from exc
    if str(provider_selection_id) != action.selection_id or provider_selection_id <= 0:
        raise ProviderEvidenceError(
            "Betfair readback requires canonical positive numeric selection_id"
        )

    candidates: list[
        tuple[str, BetfairCurrentOrderObservation | BetfairClearedOrderObservation]
    ] = []
    for order in current:
        if order.customer_order_ref == action.action_id:
            candidates.append(("current", order))
    for order in cleared:
        if order.customer_order_ref == action.action_id:
            candidates.append(("cleared", order))

    for _, order in candidates:
        if (
            order.market_id != action.market_id
            or order.selection_id != provider_selection_id
            or order.side != action.side
        ):
            raise ProviderEvidenceError(
                "provider order identity conflicts with execution action"
            )
    receipt_ids = {order.bet_id for _, order in candidates}
    if len(receipt_ids) > 1:
        raise ProviderEvidenceError(
            "multiple provider receipts claim one execution action"
        )

    if not candidates:
        evidence_id = _digest(
            {
                "schema": "autosport.betfair_execution_absence",
                "schema_version": 1,
                "bookmaker_id": action.bookmaker_id,
                "account_id": action.account_id,
                "action_id": action.action_id,
                "event_id": action.event_id,
                "market_id": action.market_id,
                "selection_id": action.selection_id,
                "current_pages_sha256": current_sha,
                "cleared_pages_sha256": cleared_sha,
                "observed_at": observed_at,
            }
        )
        return VerifiedProviderAbsenceEvidence(
            action.bookmaker_id,
            action.account_id,
            action.action_id,
            profile.adapter_id,
            profile.adapter_version,
            profile.profile_version,
            action.event_id,
            action.market_id,
            action.selection_id,
            observed_at,
            current_sha,
            cleared_sha,
            evidence_id,
            _SEAL,
        )

    # A transition can make the same bet visible in current and cleared snapshots.
    # Prefer cleared evidence, but require all appearances to use the same receipt.
    kind, order = sorted(candidates, key=lambda item: item[0] != "cleared")[0]
    if kind == "cleared":
        assert isinstance(order, BetfairClearedOrderObservation)
        accepted_stake = order.size_settled
        accepted_odds = order.price_matched
        order_at = order.evidence.observed_at
    else:
        assert isinstance(order, BetfairCurrentOrderObservation)
        accepted_stake = order.size_matched
        accepted_odds = order.average_price_matched
        order_at = order.evidence.observed_at

    if accepted_stake <= 0 or accepted_odds <= 0:
        raise ProviderEvidenceError(
            "provider order exists but matched execution economics remain unresolved"
        )
    if accepted_stake > action.requested_stake:
        raise ProviderEvidenceError("provider matched stake exceeds requested stake")
    status = (
        AcknowledgementStatus.ACCEPTED
        if accepted_stake == action.requested_stake
        else AcknowledgementStatus.PARTIAL
    )
    source_sha = _digest(
        {
            "current_pages_sha256": current_sha,
            "cleared_pages_sha256": cleared_sha,
            "receipt_id": order.bet_id,
            "order_observed_at": order_at,
        }
    )
    evidence_id = _digest(
        {
            "schema": "autosport.betfair_execution_effect",
            "schema_version": 1,
            "bookmaker_id": action.bookmaker_id,
            "account_id": action.account_id,
            "action_id": action.action_id,
            "event_id": action.event_id,
            "market_id": action.market_id,
            "selection_id": action.selection_id,
            "external_receipt_id": order.bet_id,
            "observed_at": observed_at,
            "source_payload_sha256": source_sha,
            "status": status.value,
            "accepted_odds": str(accepted_odds),
            "accepted_stake": str(accepted_stake),
        }
    )
    return VerifiedProviderEffectEvidence(
        action.bookmaker_id,
        action.account_id,
        action.action_id,
        profile.adapter_id,
        profile.adapter_version,
        profile.profile_version,
        action.event_id,
        action.market_id,
        action.selection_id,
        order.bet_id,
        observed_at,
        source_sha,
        status,
        accepted_odds,
        accepted_stake,
        evidence_id,
        _SEAL,
    )
