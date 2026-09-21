from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import Any
from weakref import ref

from .betfair_account_readonly import (
    BetfairClearedOrderObservation,
    BetfairCurrentOrderObservation,
    BetfairExecutionReadbackEnvelope,
    BetfairReadOnlyError,
)
from .real_execution_ledger import ExecutionAction


SCHEMA_VERSION = 1


class RealizedMatchEvidenceError(RuntimeError):
    """Raised when provider match economics cannot be resolved safely."""


class RealizedMatchSource(str, Enum):
    INCOMPLETE_EVIDENCE = "INCOMPLETE_EVIDENCE"
    CURRENT_ORDER = "CURRENT_ORDER"
    CLEARED_BET = "CLEARED_BET"


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RealizedMatchEvidenceError(
            "realized match evidence is not canonical JSON"
        ) from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _provider_ref(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 32
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RealizedMatchEvidenceError(
            "provider_order_ref must be <=32 lowercase hex characters"
        )
    return value


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairRealizedMatchEvidence:
    """Read-only projection of exact provider matched-price/size truth.

    This object is not a provider write, settlement, or execution-state authority.
    Positive authority is valid only when ``assert_authoritative`` succeeds.
    """

    source: RealizedMatchSource
    action_id: str
    bookmaker_id: str
    account_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    provider_order_ref: str
    bet_id: str | None
    requested_odds: Decimal
    requested_stake: Decimal
    provider_matched_odds: Decimal | None
    provider_matched_stake: Decimal | None
    unrealized_requested_stake: Decimal | None
    provider_status: str | None
    provider_observed_at: str | None
    provider_settled_at: str | None
    source_payload_sha256: str | None
    readback_observed_at: str
    readback_evidence_sha256: str
    finalized: bool
    evidence_id: str
    schema_version: int = SCHEMA_VERSION

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source": self.source.value,
            "action_id": self.action_id,
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "provider_order_ref": self.provider_order_ref,
            "bet_id": self.bet_id,
            "requested_odds": _decimal_text(self.requested_odds),
            "requested_stake": _decimal_text(self.requested_stake),
            "provider_matched_odds": (
                _decimal_text(self.provider_matched_odds)
                if self.provider_matched_odds is not None
                else None
            ),
            "provider_matched_stake": (
                _decimal_text(self.provider_matched_stake)
                if self.provider_matched_stake is not None
                else None
            ),
            "unrealized_requested_stake": (
                _decimal_text(self.unrealized_requested_stake)
                if self.unrealized_requested_stake is not None
                else None
            ),
            "provider_status": self.provider_status,
            "provider_observed_at": self.provider_observed_at,
            "provider_settled_at": self.provider_settled_at,
            "source_payload_sha256": self.source_payload_sha256,
            "readback_observed_at": self.readback_observed_at,
            "readback_evidence_sha256": self.readback_evidence_sha256,
            "finalized": self.finalized,
        }

    def to_dict(self) -> dict[str, object]:
        payload = self._payload()
        payload["evidence_id"] = self.evidence_id
        return payload

    @property
    def has_matched_economics(self) -> bool:
        return (
            self.provider_matched_stake is not None
            and self.provider_matched_stake > 0
            and self.provider_matched_odds is not None
        )

    def _validate_integrity(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RealizedMatchEvidenceError(
                "unsupported realized match evidence schema"
            )
        if self.evidence_id != _sha256(self._payload()):
            raise RealizedMatchEvidenceError(
                "realized match evidence digest mismatch"
            )

    def assert_authoritative(self) -> None:
        """Replaced below with an origin-aware validator."""
        self._validate_integrity()
        raise RealizedMatchEvidenceError(
            "realized match evidence was not issued by canonical resolver"
        )


def _identity_check(
    action: ExecutionAction,
    readback: BetfairExecutionReadbackEnvelope,
    provider_order_ref: str,
) -> int:
    try:
        readback.assert_authoritative()
    except BetfairReadOnlyError as exc:
        raise RealizedMatchEvidenceError(
            "execution readback is not canonical provider evidence"
        ) from exc

    if action.bookmaker_id != "betfair" or readback.venue_id != action.bookmaker_id:
        raise RealizedMatchEvidenceError(
            "execution action/readback bookmaker identity mismatch"
        )
    if readback.account_id != action.account_id:
        raise RealizedMatchEvidenceError(
            "execution action/readback account identity mismatch"
        )
    if readback.action_id != action.action_id:
        raise RealizedMatchEvidenceError(
            "execution action/readback action identity mismatch"
        )
    if readback.market_id != action.market_id:
        raise RealizedMatchEvidenceError(
            "execution action/readback market identity mismatch"
        )
    if readback.market_event.event_id != action.event_id:
        raise RealizedMatchEvidenceError(
            "execution action/readback event identity mismatch"
        )
    if readback.provider_order_ref != provider_order_ref:
        raise RealizedMatchEvidenceError(
            "execution readback provider order reference mismatch"
        )
    try:
        selection_id = int(action.selection_id)
    except (TypeError, ValueError) as exc:
        raise RealizedMatchEvidenceError(
            "execution action selection_id is not a Betfair integer"
        ) from exc
    if selection_id <= 0 or str(selection_id) != action.selection_id:
        raise RealizedMatchEvidenceError(
            "execution action selection_id is not canonical Betfair text"
        )
    return selection_id


def _check_common_row_identity(
    row: BetfairCurrentOrderObservation | BetfairClearedOrderObservation,
    *,
    action: ExecutionAction,
    selection_id: int,
    provider_order_ref: str,
) -> None:
    if row.market_id != action.market_id:
        raise RealizedMatchEvidenceError(
            "provider order row belongs to a different market"
        )
    if row.selection_id != selection_id:
        raise RealizedMatchEvidenceError(
            "provider order row belongs to a different selection"
        )
    if row.side != action.side:
        raise RealizedMatchEvidenceError(
            "provider order row has a different side"
        )
    if row.customer_order_ref != provider_order_ref:
        raise RealizedMatchEvidenceError(
            "provider order row lacks the exact durable customer order reference"
        )


def _flatten_rows(
    readback: BetfairExecutionReadbackEnvelope,
) -> tuple[
    tuple[BetfairCurrentOrderObservation, ...],
    tuple[BetfairClearedOrderObservation, ...],
]:
    current = tuple(
        order
        for page in readback.current_pages
        for order in page.orders
    )
    cleared = tuple(
        order
        for _status, pages in readback.cleared_pages_by_status
        for page in pages
        for order in page.orders
    )
    return current, cleared


def _make_evidence(
    *,
    source: RealizedMatchSource,
    action: ExecutionAction,
    readback: BetfairExecutionReadbackEnvelope,
    provider_order_ref: str,
    bet_id: str | None,
    provider_matched_odds: Decimal | None,
    provider_matched_stake: Decimal | None,
    provider_status: str | None,
    provider_observed_at: str | None,
    provider_settled_at: str | None,
    source_payload_sha256: str | None,
    finalized: bool,
) -> BetfairRealizedMatchEvidence:
    unrealized = (
        action.requested_stake - provider_matched_stake
        if provider_matched_stake is not None
        else None
    )
    draft = BetfairRealizedMatchEvidence(
        source=source,
        action_id=action.action_id,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        side=action.side,
        provider_order_ref=provider_order_ref,
        bet_id=bet_id,
        requested_odds=action.requested_odds,
        requested_stake=action.requested_stake,
        provider_matched_odds=provider_matched_odds,
        provider_matched_stake=provider_matched_stake,
        unrealized_requested_stake=unrealized,
        provider_status=provider_status,
        provider_observed_at=provider_observed_at,
        provider_settled_at=provider_settled_at,
        source_payload_sha256=source_payload_sha256,
        readback_observed_at=readback.observed_at,
        readback_evidence_sha256=readback.evidence_sha256,
        finalized=finalized,
        evidence_id="pending",
    )
    return replace(
        draft,
        evidence_id=_sha256(draft._payload()),
    )


def _resolve_betfair_realized_match(
    action: ExecutionAction,
    readback: BetfairExecutionReadbackEnvelope,
    *,
    expected_provider_order_ref: str,
) -> BetfairRealizedMatchEvidence:
    if not isinstance(action, ExecutionAction):
        raise TypeError("action must be ExecutionAction")
    if not isinstance(readback, BetfairExecutionReadbackEnvelope):
        raise TypeError("readback must be BetfairExecutionReadbackEnvelope")

    provider_order_ref = _provider_ref(expected_provider_order_ref)
    selection_id = _identity_check(action, readback, provider_order_ref)
    current, cleared = _flatten_rows(readback)

    for row in (*current, *cleared):
        _check_common_row_identity(
            row,
            action=action,
            selection_id=selection_id,
            provider_order_ref=provider_order_ref,
        )

    bet_ids = {row.bet_id for row in (*current, *cleared)}
    if len(bet_ids) > 1:
        raise RealizedMatchEvidenceError(
            "provider readback maps one durable order reference to multiple bet ids"
        )

    if cleared:
        if len(cleared) != 1:
            raise RealizedMatchEvidenceError(
                "provider readback contains multiple cleared BET rows"
            )
        row = cleared[0]
        if current and any(item.bet_id != row.bet_id for item in current):
            raise RealizedMatchEvidenceError(
                "current and cleared provider identities conflict"
            )
        if row.price_requested != action.requested_odds:
            raise RealizedMatchEvidenceError(
                "cleared BET requested price differs from execution action"
            )
        if row.size_settled > action.requested_stake:
            raise RealizedMatchEvidenceError(
                "cleared BET settled size exceeds requested stake"
            )
        if row.size_settled > 0 and row.price_matched <= 0:
            raise RealizedMatchEvidenceError(
                "cleared BET matched stake lacks a positive matched price"
            )
        if row.size_settled == 0 and row.price_matched != 0:
            raise RealizedMatchEvidenceError(
                "cleared BET zero settled size has a non-zero matched price"
            )
        return _make_evidence(
            source=RealizedMatchSource.CLEARED_BET,
            action=action,
            readback=readback,
            provider_order_ref=provider_order_ref,
            bet_id=row.bet_id,
            provider_matched_odds=(
                row.price_matched if row.size_settled > 0 else None
            ),
            provider_matched_stake=row.size_settled,
            provider_status=row.bet_status,
            provider_observed_at=row.evidence.observed_at,
            provider_settled_at=row.settled_date,
            source_payload_sha256=row.evidence.source_payload_sha256,
            finalized=True,
        )

    if current:
        if len(current) != 1:
            raise RealizedMatchEvidenceError(
                "provider readback contains multiple current BET rows"
            )
        row = current[0]
        if row.price is not None and row.price != action.requested_odds:
            raise RealizedMatchEvidenceError(
                "current order requested price differs from execution action"
            )
        if (
            row.requested_size is not None
            and row.requested_size != action.requested_stake
        ):
            raise RealizedMatchEvidenceError(
                "current order requested size differs from execution action"
            )
        if row.size_matched > action.requested_stake:
            raise RealizedMatchEvidenceError(
                "current order matched size exceeds requested stake"
            )
        if row.size_remaining > action.requested_stake:
            raise RealizedMatchEvidenceError(
                "current order remaining size exceeds requested stake"
            )
        if row.size_matched + row.size_remaining > action.requested_stake:
            raise RealizedMatchEvidenceError(
                "current order matched plus remaining size exceeds requested stake"
            )
        if row.size_matched > 0 and row.average_price_matched <= 0:
            raise RealizedMatchEvidenceError(
                "current matched stake lacks a positive average matched price"
            )
        if row.size_matched == 0 and row.average_price_matched != 0:
            raise RealizedMatchEvidenceError(
                "current zero matched size has a non-zero average matched price"
            )
        return _make_evidence(
            source=RealizedMatchSource.CURRENT_ORDER,
            action=action,
            readback=readback,
            provider_order_ref=provider_order_ref,
            bet_id=row.bet_id,
            provider_matched_odds=(
                row.average_price_matched if row.size_matched > 0 else None
            ),
            provider_matched_stake=row.size_matched,
            provider_status=row.status,
            provider_observed_at=row.evidence.observed_at,
            provider_settled_at=None,
            source_payload_sha256=row.evidence.source_payload_sha256,
            finalized=False,
        )

    return _make_evidence(
        source=RealizedMatchSource.INCOMPLETE_EVIDENCE,
        action=action,
        readback=readback,
        provider_order_ref=provider_order_ref,
        bet_id=None,
        provider_matched_odds=None,
        provider_matched_stake=None,
        provider_status=None,
        provider_observed_at=None,
        provider_settled_at=None,
        source_payload_sha256=None,
        finalized=False,
    )


def resolve_betfair_realized_match(
    action: ExecutionAction,
    readback: BetfairExecutionReadbackEnvelope,
    *,
    expected_provider_order_ref: str,
) -> BetfairRealizedMatchEvidence:
    """Resolve matched-price/size truth without mutating execution state."""
    return _resolve_betfair_realized_match(
        action,
        readback,
        expected_provider_order_ref=expected_provider_order_ref,
    )


def _install_realized_match_authority() -> None:
    issued: dict[int, tuple[object, str]] = {}
    raw_resolve = resolve_betfair_realized_match
    validate_integrity = BetfairRealizedMatchEvidence._validate_integrity

    def authoritative_resolve(
        action: ExecutionAction,
        readback: BetfairExecutionReadbackEnvelope,
        *,
        expected_provider_order_ref: str,
    ) -> BetfairRealizedMatchEvidence:
        evidence = raw_resolve(
            action,
            readback,
            expected_provider_order_ref=expected_provider_order_ref,
        )
        evidence_id = id(evidence)

        def forget(_weakref: object, *, key: int = evidence_id) -> None:
            issued.pop(key, None)

        issued[evidence_id] = (
            ref(evidence, forget),
            evidence.evidence_id,
        )
        return evidence

    def assert_authoritative(
        self: BetfairRealizedMatchEvidence,
    ) -> None:
        validate_integrity(self)
        record = issued.get(id(self))
        if record is None or record[0]() is not self:
            raise RealizedMatchEvidenceError(
                "realized match evidence was not issued by canonical resolver"
            )
        if record[1] != self.evidence_id:
            raise RealizedMatchEvidenceError(
                "realized match evidence changed after canonical resolution"
            )

    globals()["resolve_betfair_realized_match"] = authoritative_resolve
    BetfairRealizedMatchEvidence.assert_authoritative = assert_authoritative


_install_realized_match_authority()
del _install_realized_match_authority
