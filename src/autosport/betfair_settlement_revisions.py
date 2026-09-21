"""Durable Betfair settlement revision projection.

This module does not create provider or execution authority.  It consumes an
adapter-issued :class:`BetfairExecutionReadbackEnvelope` and a canonical
:class:`ExecutionAction`, then persists only mechanically reconciled BET-level
cleared-order observations as an append-only correction history.

Provider settlement is deliberately kept separate from execution rejection,
legal terminal-space exactness, and canonical money/cost authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Lock
from typing import Any

from .betfair_account_readonly import (
    BetfairClearedOrderObservation,
    BetfairExecutionReadbackEnvelope,
    BetfairReadOnlyError,
)
from .real_execution_ledger import ExecutionAction

_SCHEMA = "autosport.betfair_settlement_revision"
_SCHEMA_VERSION = 1
_ALLOWED_STATUSES = frozenset({"SETTLED", "VOIDED", "LAPSED", "CANCELLED"})


class BetfairSettlementRevisionError(RuntimeError):
    """Raised when settlement evidence cannot be projected safely."""


class BetfairSettlementNotObserved(BetfairSettlementRevisionError):
    """Raised when an authoritative readback has no matching cleared BET row."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise BetfairSettlementRevisionError(f"{name} must be non-empty canonical text")
    return value


def _timestamp(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairSettlementRevisionError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairSettlementRevisionError(f"{name} must be timezone-aware")
    return parsed


def _sha(value: object, name: str) -> str:
    raw = _text(value, name)
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
        raise BetfairSettlementRevisionError(f"{name} must be lowercase SHA-256 hex")
    return raw


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, Decimal):
        parsed = value
    elif type(value) is str:
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise BetfairSettlementRevisionError(f"{name} must be finite Decimal") from exc
    else:
        raise BetfairSettlementRevisionError(f"{name} must be finite Decimal")
    if not parsed.is_finite():
        raise BetfairSettlementRevisionError(f"{name} must be finite Decimal")
    return parsed


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairSettlementRevisionError("settlement evidence is not canonical JSON") from exc


def _digest(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True, slots=True)
class BetfairSettlementRevision:
    revision_id: str
    previous_revision_id: str | None
    revision_number: int
    bookmaker_id: str
    account_id: str
    action_id: str
    external_bet_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    provider_status: str
    placed_date: str
    settled_date: str
    price_requested: Decimal
    price_matched: Decimal
    size_settled: Decimal
    provider_profit: Decimal
    available_at: str
    source_payload_sha256: str
    capture_evidence_sha256: str
    content_sha256: str

    def __post_init__(self) -> None:
        _sha(self.revision_id, "revision_id")
        if self.previous_revision_id is not None:
            _sha(self.previous_revision_id, "previous_revision_id")
        if type(self.revision_number) is not int or self.revision_number < 1:
            raise BetfairSettlementRevisionError("revision_number must be a positive integer")
        for name in (
            "bookmaker_id",
            "account_id",
            "action_id",
            "external_bet_id",
            "event_id",
            "market_id",
            "selection_id",
            "side",
        ):
            _text(getattr(self, name), name)
        if self.provider_status not in _ALLOWED_STATUSES:
            raise BetfairSettlementRevisionError("provider_status is not a cleared Betfair status")
        _timestamp(self.placed_date, "placed_date")
        _timestamp(self.settled_date, "settled_date")
        _timestamp(self.available_at, "available_at")
        if _timestamp(self.available_at, "available_at") < _timestamp(self.settled_date, "settled_date"):
            raise BetfairSettlementRevisionError("settlement cannot be available before provider settled_date")
        _decimal(self.price_requested, "price_requested")
        _decimal(self.price_matched, "price_matched")
        _decimal(self.size_settled, "size_settled")
        _decimal(self.provider_profit, "provider_profit")
        _sha(self.source_payload_sha256, "source_payload_sha256")
        _sha(self.capture_evidence_sha256, "capture_evidence_sha256")
        _sha(self.content_sha256, "content_sha256")
        expected_content = _digest(self.content_payload())
        if self.content_sha256 != expected_content:
            raise BetfairSettlementRevisionError("settlement content digest mismatch")
        expected_revision = _digest(
            {
                "schema": _SCHEMA,
                "schema_version": _SCHEMA_VERSION,
                "previous_revision_id": self.previous_revision_id,
                "revision_number": self.revision_number,
                "content_sha256": self.content_sha256,
            }
        )
        if self.revision_id != expected_revision:
            raise BetfairSettlementRevisionError("settlement revision identity mismatch")

    @property
    def permanent_final(self) -> bool:
        """Betfair settlement observations never self-assert irreversible finality."""
        return False

    @property
    def terminal_space_exact(self) -> bool:
        """A realized provider receipt is not prospective legal outcome-space proof."""
        return False

    def content_payload(self) -> dict[str, str]:
        return {
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "action_id": self.action_id,
            "external_bet_id": self.external_bet_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "provider_status": self.provider_status,
            "placed_date": self.placed_date,
            "settled_date": self.settled_date,
            "price_requested": _decimal_text(self.price_requested),
            "price_matched": _decimal_text(self.price_matched),
            "size_settled": _decimal_text(self.size_settled),
            "provider_profit": _decimal_text(self.provider_profit),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "previous_revision_id": self.previous_revision_id,
            "revision_number": self.revision_number,
            **self.content_payload(),
            "available_at": self.available_at,
            "source_payload_sha256": self.source_payload_sha256,
            "capture_evidence_sha256": self.capture_evidence_sha256,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> "BetfairSettlementRevision":
        if type(value) is not dict:
            raise BetfairSettlementRevisionError("revision payload must be an object")
        expected = {
            "revision_id",
            "previous_revision_id",
            "revision_number",
            "bookmaker_id",
            "account_id",
            "action_id",
            "external_bet_id",
            "event_id",
            "market_id",
            "selection_id",
            "side",
            "provider_status",
            "placed_date",
            "settled_date",
            "price_requested",
            "price_matched",
            "size_settled",
            "provider_profit",
            "available_at",
            "source_payload_sha256",
            "capture_evidence_sha256",
            "content_sha256",
        }
        if set(value) != expected:
            raise BetfairSettlementRevisionError("revision payload fields are not canonical")
        return cls(
            revision_id=value["revision_id"],
            previous_revision_id=value["previous_revision_id"],
            revision_number=value["revision_number"],
            bookmaker_id=value["bookmaker_id"],
            account_id=value["account_id"],
            action_id=value["action_id"],
            external_bet_id=value["external_bet_id"],
            event_id=value["event_id"],
            market_id=value["market_id"],
            selection_id=value["selection_id"],
            side=value["side"],
            provider_status=value["provider_status"],
            placed_date=value["placed_date"],
            settled_date=value["settled_date"],
            price_requested=_decimal(value["price_requested"], "price_requested"),
            price_matched=_decimal(value["price_matched"], "price_matched"),
            size_settled=_decimal(value["size_settled"], "size_settled"),
            provider_profit=_decimal(value["provider_profit"], "provider_profit"),
            available_at=value["available_at"],
            source_payload_sha256=value["source_payload_sha256"],
            capture_evidence_sha256=value["capture_evidence_sha256"],
            content_sha256=value["content_sha256"],
        )


@dataclass(frozen=True, slots=True)
class SettlementIngestResult:
    revision: BetfairSettlementRevision
    created: bool


class BetfairSettlementRevisionStore:
    """Append-only durable projection of current provider settlement revisions."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._lock = Lock()
        self._revisions: list[BetfairSettlementRevision] = []
        self._by_bet: dict[tuple[str, str, str], list[BetfairSettlementRevision]] = {}
        self._last_record_sha256: str | None = None
        self._load()

    @property
    def revisions(self) -> tuple[BetfairSettlementRevision, ...]:
        with self._lock:
            return tuple(self._revisions)

    def current(
        self, bookmaker_id: str, account_id: str, external_bet_id: str
    ) -> BetfairSettlementRevision | None:
        key = (_text(bookmaker_id, "bookmaker_id"), _text(account_id, "account_id"), _text(external_bet_id, "external_bet_id"))
        with self._lock:
            chain = self._by_bet.get(key, ())
            return chain[-1] if chain else None

    def as_of(
        self,
        bookmaker_id: str,
        account_id: str,
        external_bet_id: str,
        cutoff: str,
    ) -> BetfairSettlementRevision | None:
        key = (_text(bookmaker_id, "bookmaker_id"), _text(account_id, "account_id"), _text(external_bet_id, "external_bet_id"))
        cutoff_time = _timestamp(cutoff, "cutoff")
        with self._lock:
            visible = [
                revision
                for revision in self._by_bet.get(key, ())
                if _timestamp(revision.available_at, "available_at") <= cutoff_time
            ]
            return visible[-1] if visible else None

    def ingest(
        self,
        action: ExecutionAction,
        capture: BetfairExecutionReadbackEnvelope,
    ) -> SettlementIngestResult:
        if not isinstance(action, ExecutionAction):
            raise BetfairSettlementRevisionError("action must be canonical ExecutionAction")
        if not isinstance(capture, BetfairExecutionReadbackEnvelope):
            raise BetfairSettlementRevisionError(
                "capture must be canonical BetfairExecutionReadbackEnvelope"
            )
        try:
            capture.assert_authoritative()
        except BetfairReadOnlyError as exc:
            raise BetfairSettlementRevisionError(
                "settlement capture is not canonical adapter-issued evidence"
            ) from exc

        order = _match_cleared_order(action, capture)
        content = _content_from_order(action, capture, order)
        content_sha256 = _digest(content)
        key = (action.bookmaker_id, action.account_id, order.bet_id)
        available_at = capture.observed_at
        _timestamp(available_at, "available_at")

        with self._lock:
            chain = self._by_bet.get(key, [])
            if chain:
                current = chain[-1]
                if current.action_id != action.action_id:
                    raise BetfairSettlementRevisionError(
                        "external bet identity is already bound to a different execution action"
                    )
                if content_sha256 == current.content_sha256:
                    return SettlementIngestResult(current, False)
                if _timestamp(available_at, "available_at") <= _timestamp(
                    current.available_at, "current available_at"
                ):
                    raise BetfairSettlementRevisionError(
                        "changed settlement evidence is not causally later than current revision"
                    )
                previous_revision_id = current.revision_id
                revision_number = current.revision_number + 1
            else:
                previous_revision_id = None
                revision_number = 1

            revision_id = _digest(
                {
                    "schema": _SCHEMA,
                    "schema_version": _SCHEMA_VERSION,
                    "previous_revision_id": previous_revision_id,
                    "revision_number": revision_number,
                    "content_sha256": content_sha256,
                }
            )
            revision = BetfairSettlementRevision(
                revision_id=revision_id,
                previous_revision_id=previous_revision_id,
                revision_number=revision_number,
                bookmaker_id=action.bookmaker_id,
                account_id=action.account_id,
                action_id=action.action_id,
                external_bet_id=order.bet_id,
                event_id=action.event_id,
                market_id=action.market_id,
                selection_id=action.selection_id,
                side=action.side,
                provider_status=order.bet_status,
                placed_date=order.placed_date,
                settled_date=order.settled_date,
                price_requested=order.price_requested,
                price_matched=order.price_matched,
                size_settled=order.size_settled,
                provider_profit=order.profit,
                available_at=available_at,
                source_payload_sha256=order.evidence.source_payload_sha256,
                capture_evidence_sha256=capture.evidence_sha256,
                content_sha256=content_sha256,
            )
            self._append_record(revision)
            self._accept_loaded_revision(revision)
            return SettlementIngestResult(revision, True)

    def _append_record(self, revision: BetfairSettlementRevision) -> None:
        unsigned = {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "previous_record_sha256": self._last_record_sha256,
            "revision": revision.to_dict(),
        }
        record_sha256 = _digest(unsigned)
        record = {**unsigned, "record_sha256": record_sha256}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = (_canonical(record) + "\n").encode("utf-8")
        with self._path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self._last_record_sha256 = record_sha256

    def _load(self) -> None:
        if not self._path.exists():
            return
        previous_record_sha256: str | None = None
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.endswith("\n"):
                        raise BetfairSettlementRevisionError(
                            "settlement revision log has a partial final record"
                        )
                    record = json.loads(
                        line,
                        object_pairs_hook=_strict_pairs,
                        parse_constant=_reject_constant,
                    )
                    if type(record) is not dict or set(record) != {
                        "schema",
                        "schema_version",
                        "previous_record_sha256",
                        "revision",
                        "record_sha256",
                    }:
                        raise BetfairSettlementRevisionError(
                            f"settlement log record {line_number} is not canonical"
                        )
                    if record["schema"] != _SCHEMA or record["schema_version"] != _SCHEMA_VERSION:
                        raise BetfairSettlementRevisionError(
                            f"settlement log record {line_number} has unsupported schema"
                        )
                    if record["previous_record_sha256"] != previous_record_sha256:
                        raise BetfairSettlementRevisionError(
                            f"settlement log record {line_number} breaks hash chain"
                        )
                    supplied_hash = _sha(record["record_sha256"], "record_sha256")
                    unsigned = dict(record)
                    unsigned.pop("record_sha256")
                    if supplied_hash != _digest(unsigned):
                        raise BetfairSettlementRevisionError(
                            f"settlement log record {line_number} digest mismatch"
                        )
                    revision = BetfairSettlementRevision.from_dict(record["revision"])
                    self._accept_loaded_revision(revision)
                    previous_record_sha256 = supplied_hash
        except json.JSONDecodeError as exc:
            raise BetfairSettlementRevisionError("settlement revision log is invalid JSON") from exc
        self._last_record_sha256 = previous_record_sha256

    def _accept_loaded_revision(self, revision: BetfairSettlementRevision) -> None:
        key = (revision.bookmaker_id, revision.account_id, revision.external_bet_id)
        chain = self._by_bet.setdefault(key, [])
        if chain:
            prior = chain[-1]
            if revision.action_id != prior.action_id:
                raise BetfairSettlementRevisionError(
                    "settlement revision changes bound execution action"
                )
            if revision.previous_revision_id != prior.revision_id:
                raise BetfairSettlementRevisionError(
                    "settlement revision predecessor mismatch"
                )
            if revision.revision_number != prior.revision_number + 1:
                raise BetfairSettlementRevisionError(
                    "settlement revision number is not contiguous"
                )
            if _timestamp(revision.available_at, "available_at") <= _timestamp(
                prior.available_at, "prior available_at"
            ):
                raise BetfairSettlementRevisionError(
                    "settlement correction availability is not strictly increasing"
                )
            if revision.content_sha256 == prior.content_sha256:
                raise BetfairSettlementRevisionError(
                    "duplicate settlement content must not create a new revision"
                )
        elif revision.previous_revision_id is not None or revision.revision_number != 1:
            raise BetfairSettlementRevisionError("first settlement revision has invalid predecessor")
        chain.append(revision)
        self._revisions.append(revision)


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BetfairSettlementRevisionError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise BetfairSettlementRevisionError(f"non-finite JSON value {value!r}")


def _match_cleared_order(
    action: ExecutionAction,
    capture: BetfairExecutionReadbackEnvelope,
) -> BetfairClearedOrderObservation:
    if capture.venue_id != action.bookmaker_id:
        raise BetfairSettlementRevisionError("settlement capture bookmaker mismatch")
    if capture.account_id != action.account_id:
        raise BetfairSettlementRevisionError("settlement capture account mismatch")
    if capture.action_id != action.action_id:
        raise BetfairSettlementRevisionError("settlement capture action mismatch")
    if capture.market_id != action.market_id:
        raise BetfairSettlementRevisionError("settlement capture market mismatch")
    if capture.market_event.event_id != action.event_id:
        raise BetfairSettlementRevisionError("settlement capture event mismatch")

    expected_ref = capture.provider_order_ref or action.action_id
    matches: list[BetfairClearedOrderObservation] = []
    for status, pages in capture.cleared_pages_by_status:
        if status not in _ALLOWED_STATUSES:
            raise BetfairSettlementRevisionError("capture contains unsupported cleared status")
        for page in pages:
            for order in page.orders:
                if order.bet_status != status:
                    raise BetfairSettlementRevisionError(
                        "cleared row status does not match requested provider partition"
                    )
                if order.market_id != action.market_id:
                    continue
                if str(order.selection_id) != action.selection_id:
                    continue
                if order.side != action.side:
                    continue
                if order.customer_order_ref is not None and order.customer_order_ref != expected_ref:
                    raise BetfairSettlementRevisionError(
                        "cleared row customer_order_ref mismatches execution action"
                    )
                matches.append(order)
    if not matches:
        raise BetfairSettlementNotObserved(
            "authoritative readback has no matching BET-level cleared settlement"
        )
    if len(matches) != 1:
        raise BetfairSettlementRevisionError(
            "authoritative readback contains conflicting cleared settlement rows"
        )
    return matches[0]


def _content_from_order(
    action: ExecutionAction,
    capture: BetfairExecutionReadbackEnvelope,
    order: BetfairClearedOrderObservation,
) -> dict[str, str]:
    return {
        "bookmaker_id": action.bookmaker_id,
        "account_id": action.account_id,
        "action_id": action.action_id,
        "external_bet_id": order.bet_id,
        "event_id": capture.market_event.event_id,
        "market_id": order.market_id,
        "selection_id": str(order.selection_id),
        "side": order.side,
        "provider_status": order.bet_status,
        "placed_date": order.placed_date,
        "settled_date": order.settled_date,
        "price_requested": _decimal_text(order.price_requested),
        "price_matched": _decimal_text(order.price_matched),
        "size_settled": _decimal_text(order.size_settled),
        "provider_profit": _decimal_text(order.profit),
    }
