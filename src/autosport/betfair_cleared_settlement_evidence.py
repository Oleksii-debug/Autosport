"""Provider-origin Betfair cleared-BET terminal evidence.

The canonical Betfair readback already obtains groupBy=BET rows for every terminal
status bucket. This module preserves those provider rows as one origin-sealed,
read-only evidence object without converting provider profit into net P&L, inferring
account currency, allocating commission, binding caller-local action identity, or
mutating execution/settlement state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from weakref import ref

from .betfair_account_readonly import (
    BetfairClearedOrderObservation,
    BetfairExecutionReadbackEnvelope,
    BetfairReadOnlyError,
)


SCHEMA_VERSION = 1
_TERMINAL_STATUS_ORDER = ("SETTLED", "VOIDED", "LAPSED", "CANCELLED")
_TERMINAL_STATUS_RANK = {
    status: index for index, status in enumerate(_TERMINAL_STATUS_ORDER)
}


class BetfairClearedSettlementEvidenceError(RuntimeError):
    """Raised when cleared-BET settlement evidence is ambiguous or invalid."""


def _text(value: str, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairClearedSettlementEvidenceError(
            f"{field} must be non-empty trimmed text"
        )
    return value


def _instant(value: str, field: str) -> datetime:
    _text(value, field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BetfairClearedSettlementEvidenceError(
            f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairClearedSettlementEvidenceError(
            f"{field} must be timezone-aware"
        )
    return parsed


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BetfairClearedSettlementEvidenceError(
            "provider decimal must be finite"
        )
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise BetfairClearedSettlementEvidenceError(
            "provider decimal exponent is invalid"
        )
    coefficient = "".join(str(digit) for digit in digits) or "0"
    if all(character == "0" for character in coefficient):
        return "0"
    while len(coefficient) > 1 and coefficient.endswith("0"):
        coefficient = coefficient[:-1]
        exponent += 1
    if exponent >= 0:
        text = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        if point > 0:
            text = f"{coefficient[:point]}.{coefficient[point:]}"
        else:
            text = f"0.{('0' * -point)}{coefficient}"
    return f"-{text}" if sign else text


def _row_payload(row: BetfairClearedOrderObservation) -> dict[str, object]:
    if type(row) is not BetfairClearedOrderObservation:
        raise BetfairClearedSettlementEvidenceError(
            "terminal rows must be exact BetfairClearedOrderObservation values"
        )
    if row.bet_status not in _TERMINAL_STATUS_RANK:
        raise BetfairClearedSettlementEvidenceError(
            "cleared row has unsupported terminal status"
        )
    return {
        "bet_id": row.bet_id,
        "market_id": row.market_id,
        "selection_id": row.selection_id,
        "side": row.side,
        "bet_status": row.bet_status,
        "placed_date": row.placed_date,
        "settled_date": row.settled_date,
        "price_requested": _decimal_text(row.price_requested),
        "price_matched": _decimal_text(row.price_matched),
        "size_settled": _decimal_text(row.size_settled),
        "profit": _decimal_text(row.profit),
        "customer_order_ref": row.customer_order_ref,
        "customer_strategy_ref": row.customer_strategy_ref,
        "event_id": row.event_id,
        "observed_at": row.evidence.observed_at,
        "source_payload_sha256": row.evidence.source_payload_sha256,
    }


def _sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairClearedBetSettlementEvidence:
    """One origin-sealed provider terminal view for one unambiguous Betfair bet.

    terminal_rows preserves status-specific provider rows instead of pretending
    there is always exactly one cleared row. profit remains the signed BET-level
    provider field. Betfair commission is a separate MARKET-level authority and this
    object intentionally carries no account-currency or local-action assertion.
    """

    venue_id: str
    account_id: str
    adapter_id: str
    adapter_version: str
    # Compatibility slot only. Provider-origin evidence must never bind a local
    # action id; ledger/action association belongs to canonical reconciliation.
    action_id: None
    provider_order_ref: str
    event_id: str
    market_id: str
    bet_id: str
    selection_id: int
    side: str
    terminal_rows: tuple[BetfairClearedOrderObservation, ...]
    readback_observed_at: str
    request_scope_sha256: str
    readback_evidence_sha256: str
    evidence_id: str
    schema_version: int = SCHEMA_VERSION

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "venue_id": self.venue_id,
            "account_id": self.account_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "provider_order_ref": self.provider_order_ref,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "bet_id": self.bet_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "terminal_rows": [
                _row_payload(row) for row in self.terminal_rows
            ],
            "readback_observed_at": self.readback_observed_at,
            "request_scope_sha256": self.request_scope_sha256,
            "readback_evidence_sha256": self.readback_evidence_sha256,
            "currency_code": None,
            "commission": None,
        }

    def _validate_integrity(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise BetfairClearedSettlementEvidenceError(
                "unsupported cleared settlement evidence schema"
            )
        if self.action_id is not None:
            raise BetfairClearedSettlementEvidenceError(
                "provider settlement evidence must not bind local action_id"
            )
        for field in (
            "venue_id",
            "account_id",
            "adapter_id",
            "adapter_version",
            "provider_order_ref",
            "event_id",
            "market_id",
            "bet_id",
            "side",
            "request_scope_sha256",
            "readback_evidence_sha256",
            "evidence_id",
        ):
            _text(getattr(self, field), field)
        for field in ("request_scope_sha256", "readback_evidence_sha256"):
            digest = getattr(self, field)
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise BetfairClearedSettlementEvidenceError(
                    f"{field} must be lowercase SHA-256"
                )
        if type(self.selection_id) is not int or self.selection_id <= 0:
            raise BetfairClearedSettlementEvidenceError(
                "selection_id must be a positive integer"
            )
        if self.side not in {"BACK", "LAY"}:
            raise BetfairClearedSettlementEvidenceError(
                "side must be BACK or LAY"
            )
        _instant(self.readback_observed_at, "readback_observed_at")
        if type(self.terminal_rows) is not tuple or not self.terminal_rows:
            raise BetfairClearedSettlementEvidenceError(
                "terminal_rows must be a non-empty tuple"
            )
        for row in self.terminal_rows:
            _row_payload(row)
        if tuple(
            sorted(
                self.terminal_rows,
                key=lambda row: _TERMINAL_STATUS_RANK[row.bet_status],
            )
        ) != self.terminal_rows:
            raise BetfairClearedSettlementEvidenceError(
                "terminal_rows are not in canonical status order"
            )
        seen_statuses: set[str] = set()
        for row in self.terminal_rows:
            if row.bet_status in seen_statuses:
                raise BetfairClearedSettlementEvidenceError(
                    "terminal evidence contains duplicate status rows"
                )
            seen_statuses.add(row.bet_status)
            if (
                row.bet_id != self.bet_id
                or row.market_id != self.market_id
                or row.selection_id != self.selection_id
                or row.side != self.side
                or row.customer_order_ref != self.provider_order_ref
            ):
                raise BetfairClearedSettlementEvidenceError(
                    "terminal row identity does not match evidence identity"
                )
            if row.event_id is not None and row.event_id != self.event_id:
                raise BetfairClearedSettlementEvidenceError(
                    "terminal row event identity does not match evidence"
                )
            if _instant(
                row.evidence.observed_at,
                "terminal row observed_at",
            ) > _instant(
                self.readback_observed_at,
                "readback_observed_at",
            ):
                raise BetfairClearedSettlementEvidenceError(
                    "terminal row cannot be observed after enclosing readback"
                )
        if self.evidence_id != _sha256(self._payload()):
            raise BetfairClearedSettlementEvidenceError(
                "cleared settlement evidence digest mismatch"
            )

    @property
    def settled_gross_profit(self) -> Decimal | None:
        """Return provider BET-level SETTLED profit before separate commission authority."""

        for row in self.terminal_rows:
            if row.bet_status == "SETTLED":
                return row.profit
        return None

    @property
    def account_currency_bound(self) -> bool:
        """This evidence intentionally does not bind provider account currency."""

        return False

    def assert_authoritative(self) -> None:
        """Replaced below with the product-owned origin validator."""

        self._validate_integrity()
        raise BetfairClearedSettlementEvidenceError(
            "cleared settlement evidence was not issued by canonical resolver"
        )


def _resolve_betfair_cleared_bet_settlement(
    readback: BetfairExecutionReadbackEnvelope,
) -> BetfairClearedBetSettlementEvidence | None:
    if type(readback) is not BetfairExecutionReadbackEnvelope:
        raise TypeError(
            "readback must be an exact BetfairExecutionReadbackEnvelope"
        )
    try:
        readback.assert_authoritative()
    except BetfairReadOnlyError as exc:
        raise BetfairClearedSettlementEvidenceError(
            "execution readback is not canonical provider evidence"
        ) from exc

    provider_order_ref = readback.provider_order_ref
    if provider_order_ref is None:
        raise BetfairClearedSettlementEvidenceError(
            "cleared settlement evidence requires durable provider_order_ref"
        )
    _text(provider_order_ref, "provider_order_ref")

    rows = tuple(
        row
        for _status, pages in readback.cleared_pages_by_status
        for page in pages
        for row in page.orders
    )
    if not rows:
        return None

    bet_ids = {row.bet_id for row in rows}
    if len(bet_ids) != 1:
        raise BetfairClearedSettlementEvidenceError(
            "readback maps one durable provider order ref to multiple bet ids"
        )
    bet_id = next(iter(bet_ids))
    first = rows[0]
    event_id = readback.market_event.event_id

    by_status: dict[str, BetfairClearedOrderObservation] = {}
    for row in rows:
        _row_payload(row)
        if row.bet_status in by_status:
            raise BetfairClearedSettlementEvidenceError(
                "readback contains duplicate cleared row for one terminal status"
            )
        if row.market_id != readback.market_id:
            raise BetfairClearedSettlementEvidenceError(
                "cleared row belongs to a different market"
            )
        if (
            row.selection_id != first.selection_id
            or row.side != first.side
        ):
            raise BetfairClearedSettlementEvidenceError(
                "cleared rows disagree on selection or side"
            )
        if row.customer_order_ref != provider_order_ref:
            raise BetfairClearedSettlementEvidenceError(
                "cleared row lacks exact durable provider order reference"
            )
        if row.event_id is not None and row.event_id != event_id:
            raise BetfairClearedSettlementEvidenceError(
                "cleared row event identity conflicts with market evidence"
            )
        if _instant(
            row.evidence.observed_at,
            "cleared row observed_at",
        ) > _instant(
            readback.observed_at,
            "readback.observed_at",
        ):
            raise BetfairClearedSettlementEvidenceError(
                "cleared row observation is later than enclosing readback"
            )
        by_status[row.bet_status] = row

    ordered = tuple(
        by_status[status]
        for status in _TERMINAL_STATUS_ORDER
        if status in by_status
    )
    draft = BetfairClearedBetSettlementEvidence(
        venue_id=readback.venue_id,
        account_id=readback.account_id,
        adapter_id=readback.adapter_id,
        adapter_version=readback.adapter_version,
        action_id=None,
        provider_order_ref=provider_order_ref,
        event_id=event_id,
        market_id=readback.market_id,
        bet_id=bet_id,
        selection_id=first.selection_id,
        side=first.side,
        terminal_rows=ordered,
        readback_observed_at=readback.observed_at,
        request_scope_sha256=readback.request_scope_sha256,
        readback_evidence_sha256=readback.evidence_sha256,
        evidence_id="pending",
    )
    evidence = replace(draft, evidence_id=_sha256(draft._payload()))
    evidence._validate_integrity()
    return evidence


def resolve_betfair_cleared_bet_settlement(
    readback: BetfairExecutionReadbackEnvelope,
) -> BetfairClearedBetSettlementEvidence | None:
    """Resolve provider terminal BET evidence without aggregating economic meaning."""

    return _resolve_betfair_cleared_bet_settlement(readback)


def _install_cleared_settlement_authority() -> None:
    issued: dict[int, tuple[object, str]] = {}
    raw_resolve = resolve_betfair_cleared_bet_settlement
    validate_integrity = BetfairClearedBetSettlementEvidence._validate_integrity

    def authoritative_resolve(
        readback: BetfairExecutionReadbackEnvelope,
    ) -> BetfairClearedBetSettlementEvidence | None:
        evidence = raw_resolve(readback)
        if evidence is None:
            return None
        evidence_identity = id(evidence)

        def forget(_weakref: object, *, key: int = evidence_identity) -> None:
            issued.pop(key, None)

        issued[evidence_identity] = (
            ref(evidence, forget),
            evidence.evidence_id,
        )
        return evidence

    def assert_authoritative(
        self: BetfairClearedBetSettlementEvidence,
    ) -> None:
        validate_integrity(self)
        record = issued.get(id(self))
        if record is None or record[0]() is not self:
            raise BetfairClearedSettlementEvidenceError(
                "cleared settlement evidence was not issued by canonical resolver"
            )
        if record[1] != self.evidence_id:
            raise BetfairClearedSettlementEvidenceError(
                "cleared settlement evidence changed after canonical resolution"
            )

    globals()["resolve_betfair_cleared_bet_settlement"] = authoritative_resolve
    BetfairClearedBetSettlementEvidence.assert_authoritative = assert_authoritative


_install_cleared_settlement_authority()
del _install_cleared_settlement_authority