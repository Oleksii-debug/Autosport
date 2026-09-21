"""Causal history for authenticated Betfair account-statement captures.

The canonical billing reader owns provider I/O. This module only composes its exact page
observations into immutable, pagination-complete statement views. Later provider views may
restate earlier rows, so history is versioned and queried with as_known_at() rather than
naively summing rows across fetches.

The upstream statement DTO currently retains only the digest of itemClassData. Therefore
this module intentionally does not classify RESULT_ERR, RESULT_FIX, COMMISSION_REVERSAL,
or compute P&L/net money. That requires a separately provider-bound semantic projection.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re

from .betfair_provider_billing_inputs import BetfairProviderBillingInputsObservation


_SCHEMA = "autosport.betfair_statement_history"
_VERSION = 1
_SHA = re.compile(r"^[0-9a-f]{64}$")


class BetfairStatementHistoryError(ValueError):
    pass


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise BetfairStatementHistoryError(f"{field} must be canonical text")
    return value


def _hash(value: object) -> str:
    try:
        raw = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairStatementHistoryError("history value is not canonical JSON") from exc
    return sha256(raw).hexdigest()


def _sha(value: object, field: str) -> str:
    text = _text(value, field)
    if _SHA.fullmatch(text) is None:
        raise BetfairStatementHistoryError(f"{field} must be lowercase SHA-256")
    return text


def _instant(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairStatementHistoryError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairStatementHistoryError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _nat(value: object, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise BetfairStatementHistoryError(f"{field} must be a {qualifier} integer")
    return value


def _decimal(value: object, field: str) -> Decimal:
    if type(value) is Decimal:
        result = value
    elif type(value) is str and value == value.strip() and value:
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise BetfairStatementHistoryError(f"{field} must be Decimal") from exc
    else:
        raise BetfairStatementHistoryError(f"{field} must be Decimal")
    if not result.is_finite():
        raise BetfairStatementHistoryError(f"{field} must be finite")
    return result


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise BetfairStatementHistoryError(f"{field} must be a JSON object")
    return value


def _keys(value: Mapping[str, object], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise BetfairStatementHistoryError(f"{field} has invalid fields")


@dataclass(frozen=True, slots=True)
class BetfairStatementRow:
    ordinal: int
    ref_id: str
    item_date: str
    amount: Decimal
    balance: Decimal
    item_class: str
    item_class_data_sha256: str

    def __post_init__(self) -> None:
        _nat(self.ordinal, "row.ordinal")
        _text(self.ref_id, "row.ref_id")
        _instant(self.item_date, "row.item_date")
        _decimal(self.amount, "row.amount")
        _decimal(self.balance, "row.balance")
        _text(self.item_class, "row.item_class")
        _sha(self.item_class_data_sha256, "row.item_class_data_sha256")

    def as_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "ref_id": self.ref_id,
            "item_date": self.item_date,
            "amount": str(self.amount),
            "balance": str(self.balance),
            "item_class": self.item_class,
            "item_class_data_sha256": self.item_class_data_sha256,
        }


@dataclass(frozen=True, slots=True)
class BetfairStatementPageProof:
    page_index: int
    from_record: int
    record_count: int
    row_count: int
    request_scope_sha256: str
    provider_observation_sha256: str
    statement_payload_sha256: str
    observed_at: str
    more_available: bool

    def __post_init__(self) -> None:
        _nat(self.page_index, "page.page_index")
        _nat(self.from_record, "page.from_record")
        _nat(self.record_count, "page.record_count", positive=True)
        _nat(self.row_count, "page.row_count")
        if self.row_count > self.record_count:
            raise BetfairStatementHistoryError("page has more rows than requested")
        _sha(self.request_scope_sha256, "page.request_scope_sha256")
        _sha(self.provider_observation_sha256, "page.provider_observation_sha256")
        _sha(self.statement_payload_sha256, "page.statement_payload_sha256")
        _instant(self.observed_at, "page.observed_at")
        if type(self.more_available) is not bool:
            raise BetfairStatementHistoryError("page.more_available must be bool")

    def as_dict(self) -> dict[str, object]:
        return {
            "page_index": self.page_index,
            "from_record": self.from_record,
            "record_count": self.record_count,
            "row_count": self.row_count,
            "request_scope_sha256": self.request_scope_sha256,
            "provider_observation_sha256": self.provider_observation_sha256,
            "statement_payload_sha256": self.statement_payload_sha256,
            "observed_at": self.observed_at,
            "more_available": self.more_available,
        }


@dataclass(frozen=True, slots=True, init=False)
class BetfairStatementSnapshot:
    venue_id: str
    provider_owner: str
    app_id: int
    app_version_id: int
    entitlement_projection_sha256: str
    currency_code: str
    statement_from: str | None
    statement_to: str | None
    available_at: str
    pages: tuple[BetfairStatementPageProof, ...]
    rows: tuple[BetfairStatementRow, ...]
    predecessor_snapshot_id: str | None
    scope_id: str
    ordered_rows_sha256: str
    source_capture_sha256: str
    snapshot_id: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("BetfairStatementSnapshot is history-derived")

    @property
    def pagination_complete(self) -> bool:
        return True

    @property
    def cross_page_atomicity_proven(self) -> bool:
        return len(self.pages) == 1

    @property
    def economic_classification_complete(self) -> bool:
        return False

    @property
    def economic_total_authoritative(self) -> bool:
        return False

    def as_dict(self) -> dict[str, object]:
        return {
            "venue_id": self.venue_id,
            "provider_owner": self.provider_owner,
            "app_id": self.app_id,
            "app_version_id": self.app_version_id,
            "entitlement_projection_sha256": self.entitlement_projection_sha256,
            "currency_code": self.currency_code,
            "statement_from": self.statement_from,
            "statement_to": self.statement_to,
            "available_at": self.available_at,
            "pages": [page.as_dict() for page in self.pages],
            "rows": [row.as_dict() for row in self.rows],
            "predecessor_snapshot_id": self.predecessor_snapshot_id,
            "scope_id": self.scope_id,
            "ordered_rows_sha256": self.ordered_rows_sha256,
            "source_capture_sha256": self.source_capture_sha256,
            "snapshot_id": self.snapshot_id,
        }


def _scope(snapshot: dict[str, object]) -> str:
    return _hash(
        {
            key: snapshot[key]
            for key in (
                "venue_id",
                "provider_owner",
                "app_id",
                "app_version_id",
                "entitlement_projection_sha256",
                "currency_code",
                "statement_from",
                "statement_to",
            )
        }
    )


def _new_snapshot(
    *,
    metadata: dict[str, object],
    pages: tuple[BetfairStatementPageProof, ...],
    rows: tuple[BetfairStatementRow, ...],
    predecessor_snapshot_id: str | None,
    expected: Mapping[str, object] | None = None,
) -> BetfairStatementSnapshot:
    data = dict(metadata)
    data["pages"] = [page.as_dict() for page in pages]
    data["rows"] = [row.as_dict() for row in rows]
    data["predecessor_snapshot_id"] = predecessor_snapshot_id
    data["scope_id"] = _scope(data)
    data["ordered_rows_sha256"] = _hash(data["rows"])
    data["source_capture_sha256"] = _hash(data["pages"])
    data["snapshot_id"] = _hash(data)
    if expected is not None:
        for field in ("scope_id", "ordered_rows_sha256", "source_capture_sha256", "snapshot_id"):
            if data[field] != _sha(expected[field], f"snapshot.{field}"):
                raise BetfairStatementHistoryError(f"snapshot {field} mismatch")
    instance = object.__new__(BetfairStatementSnapshot)
    typed_values = {
        **data,
        "pages": pages,
        "rows": rows,
    }
    for field in BetfairStatementSnapshot.__dataclass_fields__:
        object.__setattr__(instance, field, typed_values[field])
    return instance


def _capture(
    source_pages: Iterable[BetfairProviderBillingInputsObservation],
    predecessor_snapshot_id: str | None,
) -> BetfairStatementSnapshot:
    observations = tuple(source_pages)
    if not observations or any(
        type(page) is not BetfairProviderBillingInputsObservation for page in observations
    ):
        raise BetfairStatementHistoryError(
            "pages must be non-empty exact BetfairProviderBillingInputsObservation values"
        )
    first = observations[0]
    e = first.entitlement
    s = first.statement
    metadata: dict[str, object] = {
        "venue_id": e.venue_id,
        "provider_owner": e.provider_owner,
        "app_id": e.app_id,
        "app_version_id": e.version_id,
        "entitlement_projection_sha256": e.source_projection_sha256,
        "currency_code": s.currency_code,
        "statement_from": s.statement_from,
        "statement_to": s.statement_to,
        "available_at": "",
    }
    entitlement_key = (
        e.venue_id,
        e.provider_owner,
        e.app_id,
        e.version_id,
        e.source_projection_sha256,
    )
    window_key = (s.venue_id, s.currency_code, s.statement_from, s.statement_to)
    expected_offset = 0
    previous_time: datetime | None = None
    proofs: list[BetfairStatementPageProof] = []
    rows: list[BetfairStatementRow] = []

    for index, observation in enumerate(observations):
        entitlement = observation.entitlement
        statement = observation.statement
        if (
            entitlement.venue_id,
            entitlement.provider_owner,
            entitlement.app_id,
            entitlement.version_id,
            entitlement.source_projection_sha256,
        ) != entitlement_key:
            raise BetfairStatementHistoryError("pages disagree on entitlement scope")
        if (
            statement.venue_id,
            statement.currency_code,
            statement.statement_from,
            statement.statement_to,
        ) != window_key:
            raise BetfairStatementHistoryError("pages disagree on statement scope")
        if statement.from_record != expected_offset:
            raise BetfairStatementHistoryError("statement pagination is not contiguous from zero")

        last = index == len(observations) - 1
        if last and statement.more_available:
            raise BetfairStatementHistoryError(
                "partial pagination cannot publish a complete statement snapshot"
            )
        if not last:
            if not statement.more_available:
                raise BetfairStatementHistoryError("page appears after provider completion")
            if len(statement.items) != statement.record_count:
                raise BetfairStatementHistoryError(
                    "nonterminal short page cannot prove pagination continuity"
                )

        observed = _instant(observation.observed_at, "page.observed_at")
        if previous_time is not None and observed < previous_time:
            raise BetfairStatementHistoryError("page observations move backwards in time")
        previous_time = observed
        proofs.append(
            BetfairStatementPageProof(
                index,
                statement.from_record,
                statement.record_count,
                len(statement.items),
                statement.request_scope_sha256,
                observation.evidence_sha256,
                statement.statement_evidence.source_payload_sha256,
                observation.observed_at,
                statement.more_available,
            )
        )
        for item in statement.items:
            rows.append(
                BetfairStatementRow(
                    len(rows),
                    item.ref_id,
                    item.item_date,
                    item.amount,
                    item.balance,
                    item.item_class,
                    item.item_class_data_sha256,
                )
            )
        expected_offset = statement.from_record + statement.record_count

    metadata["available_at"] = max(
        (page.observed_at for page in observations),
        key=lambda value: _instant(value, "page.observed_at"),
    )
    return _new_snapshot(
        metadata=metadata,
        pages=tuple(proofs),
        rows=tuple(rows),
        predecessor_snapshot_id=predecessor_snapshot_id,
    )


def _row_from_dict(value: object) -> BetfairStatementRow:
    d = _object(value, "row")
    _keys(
        d,
        {
            "ordinal",
            "ref_id",
            "item_date",
            "amount",
            "balance",
            "item_class",
            "item_class_data_sha256",
        },
        "row",
    )
    return BetfairStatementRow(
        _nat(d["ordinal"], "row.ordinal"),
        _text(d["ref_id"], "row.ref_id"),
        _text(d["item_date"], "row.item_date"),
        _decimal(d["amount"], "row.amount"),
        _decimal(d["balance"], "row.balance"),
        _text(d["item_class"], "row.item_class"),
        _sha(d["item_class_data_sha256"], "row.item_class_data_sha256"),
    )


def _page_from_dict(value: object) -> BetfairStatementPageProof:
    d = _object(value, "page")
    _keys(
        d,
        {
            "page_index",
            "from_record",
            "record_count",
            "row_count",
            "request_scope_sha256",
            "provider_observation_sha256",
            "statement_payload_sha256",
            "observed_at",
            "more_available",
        },
        "page",
    )
    if type(d["more_available"]) is not bool:
        raise BetfairStatementHistoryError("page.more_available must be bool")
    return BetfairStatementPageProof(
        _nat(d["page_index"], "page.page_index"),
        _nat(d["from_record"], "page.from_record"),
        _nat(d["record_count"], "page.record_count", positive=True),
        _nat(d["row_count"], "page.row_count"),
        _sha(d["request_scope_sha256"], "page.request_scope_sha256"),
        _sha(d["provider_observation_sha256"], "page.provider_observation_sha256"),
        _sha(d["statement_payload_sha256"], "page.statement_payload_sha256"),
        _text(d["observed_at"], "page.observed_at"),
        d["more_available"],
    )


def _snapshot_from_dict(value: object) -> BetfairStatementSnapshot:
    d = _object(value, "snapshot")
    fields = set(BetfairStatementSnapshot.__dataclass_fields__)
    _keys(d, fields, "snapshot")
    raw_pages = d["pages"]
    raw_rows = d["rows"]
    if type(raw_pages) is not list or type(raw_rows) is not list:
        raise BetfairStatementHistoryError("snapshot pages/rows must be JSON arrays")
    predecessor = d["predecessor_snapshot_id"]
    if predecessor is not None:
        predecessor = _sha(predecessor, "snapshot.predecessor_snapshot_id")
    metadata = {
        "venue_id": _text(d["venue_id"], "snapshot.venue_id"),
        "provider_owner": _text(d["provider_owner"], "snapshot.provider_owner"),
        "app_id": _nat(d["app_id"], "snapshot.app_id", positive=True),
        "app_version_id": _nat(d["app_version_id"], "snapshot.app_version_id", positive=True),
        "entitlement_projection_sha256": _sha(
            d["entitlement_projection_sha256"], "snapshot.entitlement_projection_sha256"
        ),
        "currency_code": _text(d["currency_code"], "snapshot.currency_code"),
        "statement_from": d["statement_from"],
        "statement_to": d["statement_to"],
        "available_at": _text(d["available_at"], "snapshot.available_at"),
    }
    if metadata["statement_from"] is not None:
        _instant(metadata["statement_from"], "snapshot.statement_from")
    if metadata["statement_to"] is not None:
        _instant(metadata["statement_to"], "snapshot.statement_to")
    _instant(metadata["available_at"], "snapshot.available_at")
    return _new_snapshot(
        metadata=metadata,
        pages=tuple(_page_from_dict(item) for item in raw_pages),
        rows=tuple(_row_from_dict(item) for item in raw_rows),
        predecessor_snapshot_id=predecessor,
        expected=d,
    )


@dataclass(frozen=True, slots=True, init=False)
class BetfairStatementHistory:
    snapshots: tuple[BetfairStatementSnapshot, ...]

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("use empty() or from_json()")

    @classmethod
    def empty(cls) -> "BetfairStatementHistory":
        instance = object.__new__(cls)
        object.__setattr__(instance, "snapshots", ())
        return instance

    @classmethod
    def _validated(
        cls, snapshots: tuple[BetfairStatementSnapshot, ...]
    ) -> "BetfairStatementHistory":
        seen: set[str] = set()
        scope: str | None = None
        previous: BetfairStatementSnapshot | None = None
        for snapshot in snapshots:
            if snapshot.source_capture_sha256 in seen:
                raise BetfairStatementHistoryError("history contains duplicate capture")
            seen.add(snapshot.source_capture_sha256)
            scope = snapshot.scope_id if scope is None else scope
            if snapshot.scope_id != scope:
                raise BetfairStatementHistoryError("history contains mixed statement scopes")
            expected = None if previous is None else previous.snapshot_id
            if snapshot.predecessor_snapshot_id != expected:
                raise BetfairStatementHistoryError("snapshot predecessor chain is broken")
            if previous is not None and _instant(
                snapshot.available_at, "snapshot.available_at"
            ) <= _instant(previous.available_at, "previous.available_at"):
                raise BetfairStatementHistoryError(
                    "distinct captures require increasing causal availability"
                )
            previous = snapshot
        instance = object.__new__(cls)
        object.__setattr__(instance, "snapshots", snapshots)
        return instance

    @property
    def history_id(self) -> str:
        return _hash(self.as_dict())

    def append_pages(
        self, pages: Iterable[BetfairProviderBillingInputsObservation]
    ) -> "BetfairStatementHistory":
        predecessor = self.snapshots[-1].snapshot_id if self.snapshots else None
        candidate = _capture(pages, predecessor)
        if any(
            item.source_capture_sha256 == candidate.source_capture_sha256
            for item in self.snapshots
        ):
            return self
        if self.snapshots:
            if candidate.scope_id != self.snapshots[0].scope_id:
                raise BetfairStatementHistoryError("new capture changes statement scope")
            if _instant(candidate.available_at, "candidate.available_at") <= _instant(
                self.snapshots[-1].available_at, "latest.available_at"
            ):
                raise BetfairStatementHistoryError(
                    "new capture does not advance causal availability"
                )
        return self._validated(self.snapshots + (candidate,))

    def current_restated_view(self) -> BetfairStatementSnapshot:
        if not self.snapshots:
            raise BetfairStatementHistoryError("statement history is empty")
        return self.snapshots[-1]

    def as_known_at(self, causal_cutoff: str) -> BetfairStatementSnapshot:
        cutoff = _instant(causal_cutoff, "causal_cutoff")
        for snapshot in reversed(self.snapshots):
            if _instant(snapshot.available_at, "snapshot.available_at") <= cutoff:
                return snapshot
        raise BetfairStatementHistoryError(
            "no complete statement snapshot was causally available at cutoff"
        )

    def latest_capture_is_restatement(self) -> bool:
        return len(self.snapshots) > 1 and (
            self.snapshots[-1].ordered_rows_sha256
            != self.snapshots[-2].ordered_rows_sha256
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "schema_version": _VERSION,
            "snapshots": [snapshot.as_dict() for snapshot in self.snapshots],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

    @classmethod
    def from_json(cls, payload: str) -> "BetfairStatementHistory":
        if type(payload) is not str:
            raise BetfairStatementHistoryError("history payload must be text")

        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in items:
                if key in result:
                    raise BetfairStatementHistoryError("history JSON has duplicate key")
                result[key] = value
            return result

        def constant(_value: str) -> object:
            raise BetfairStatementHistoryError("history JSON has invalid numeric constant")

        try:
            raw = json.loads(payload, object_pairs_hook=pairs, parse_constant=constant)
        except BetfairStatementHistoryError:
            raise
        except json.JSONDecodeError as exc:
            raise BetfairStatementHistoryError("history payload is not valid JSON") from exc

        data = _object(raw, "history")
        _keys(data, {"schema", "schema_version", "snapshots"}, "history")
        if (
            data["schema"] != _SCHEMA
            or isinstance(data["schema_version"], bool)
            or data["schema_version"] != _VERSION
        ):
            raise BetfairStatementHistoryError("unsupported history schema")
        raw_snapshots = data["snapshots"]
        if type(raw_snapshots) is not list:
            raise BetfairStatementHistoryError("history snapshots must be JSON array")
        return cls._validated(tuple(_snapshot_from_dict(item) for item in raw_snapshots))
