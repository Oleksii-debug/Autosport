from __future__ import annotations

import json
import re
import sqlite3
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from threading import RLock, local
from typing import Iterable

from .domain import MarketEvent


_HISTORY_COLUMNS = (
    "dedupe_key",
    "quote_key",
    "event_id",
    "market_id",
    "selection_id",
    "decimal_odds",
    "observed_ts",
    "source_id",
    "sequence",
    "payload_json",
)
_HISTORY_COLUMNS_SQL = ",".join(_HISTORY_COLUMNS)
_CURRENT_COLUMNS = ("source_id", "quote_key", "observed_ts", "sequence", "payload_json")
_CURRENT_COLUMNS_SQL = ",".join(_CURRENT_COLUMNS)
_LIVE_RECEIPT_COLUMNS = ("dedupe_key", "ingest_ts", "authority")
_LIVE_RECEIPT_COLUMNS_SQL = ",".join(_LIVE_RECEIPT_COLUMNS)
_LIVE_RECEIPT_AUTHORITY = "autosport.live_ingestion_receipt.v1"


def _decode_market_event(
    raw: dict[str, object],
    *,
    _event_type: type[MarketEvent] = MarketEvent,
    _from_dict=MarketEvent.from_dict,
) -> MarketEvent:
    """Decode through the import-time canonical MarketEvent codec descriptor."""
    event = _from_dict(raw)
    if type(event) is not _event_type:
        raise TypeError("canonical market event decoder returned a non-canonical type")
    return event


def _encode_market_event(
    event: MarketEvent,
    *,
    _to_dict=MarketEvent.to_dict,
) -> dict[str, object]:
    """Encode through the import-time canonical MarketEvent codec descriptor."""
    return _to_dict(event)


def _market_event_quote_key(
    event: MarketEvent,
    *,
    _getter=MarketEvent.quote_key.fget,
) -> str:
    """Read canonical quote identity without mutable descriptor dispatch."""
    if _getter is None:
        raise RuntimeError("canonical MarketEvent.quote_key descriptor is unavailable")
    return _getter(event)


def _market_event_dedupe_key(
    event: MarketEvent,
    *,
    _getter=MarketEvent.dedupe_key.fget,
) -> str:
    """Read canonical dedupe identity without mutable descriptor dispatch."""
    if _getter is None:
        raise RuntimeError("canonical MarketEvent.dedupe_key descriptor is unavailable")
    return _getter(event)


_LEGACY_CURRENT_COLUMNS = ("quote_key", "observed_ts", "sequence", "payload_json")
_LEGACY_CURRENT_COLUMNS_SQL = ",".join(_LEGACY_CURRENT_COLUMNS)

_EXPECTED_TABLE_XINFO = {
    "market_events": (
        (0, "dedupe_key", "TEXT", 0, None, 1, 0),
        (1, "quote_key", "TEXT", 1, None, 0, 0),
        (2, "event_id", "TEXT", 1, None, 0, 0),
        (3, "market_id", "TEXT", 1, None, 0, 0),
        (4, "selection_id", "TEXT", 1, None, 0, 0),
        (5, "decimal_odds", "TEXT", 1, None, 0, 0),
        (6, "observed_ts", "TEXT", 1, None, 0, 0),
        (7, "source_id", "TEXT", 1, None, 0, 0),
        (8, "sequence", "INTEGER", 1, None, 0, 0),
        (9, "payload_json", "TEXT", 1, None, 0, 0),
    ),
    "current_quotes": (
        (0, "source_id", "TEXT", 1, None, 1, 0),
        (1, "quote_key", "TEXT", 1, None, 2, 0),
        (2, "observed_ts", "TEXT", 1, None, 0, 0),
        (3, "sequence", "INTEGER", 1, None, 0, 0),
        (4, "payload_json", "TEXT", 1, None, 0, 0),
    ),
    "market_event_live_receipts": (
        (0, "dedupe_key", "TEXT", 0, None, 1, 0),
        (1, "ingest_ts", "TEXT", 1, None, 0, 0),
        (2, "authority", "TEXT", 1, None, 0, 0),
    ),
    "trusted_live_current_quotes": (
        (0, "source_id", "TEXT", 1, None, 1, 0),
        (1, "quote_key", "TEXT", 1, None, 2, 0),
        (2, "sequence", "INTEGER", 1, None, 0, 0),
        (3, "dedupe_key", "TEXT", 1, None, 0, 0),
    ),
}
_LEGACY_CURRENT_XINFO = (
    (0, "quote_key", "TEXT", 0, None, 1, 0),
    (1, "observed_ts", "TEXT", 1, None, 0, 0),
    (2, "sequence", "INTEGER", 1, None, 0, 0),
    (3, "payload_json", "TEXT", 1, None, 0, 0),
)
_EXPECTED_PRIMARY_KEYS = {
    "market_events": ("dedupe_key",),
    "current_quotes": ("source_id", "quote_key"),
    "market_event_live_receipts": ("dedupe_key",),
    "trusted_live_current_quotes": ("source_id", "quote_key"),
}
_LEGACY_CURRENT_PRIMARY_KEYS = ("quote_key",)
_CANONICAL_SECONDARY_INDEXES = {
    "idx_market_events_order": ("observed_ts", "sequence"),
    "idx_market_events_event": ("event_id", "observed_ts", "sequence"),
    "idx_market_events_quote": ("quote_key", "observed_ts", "sequence"),
}
_FORBIDDEN_TABLE_SQL = re.compile(
    r"\b(?:CHECK|COLLATE|GENERATED|REFERENCES)\b|\bON\s+CONFLICT\b",
    re.IGNORECASE,
)
_SQLITE_INTEGER_MIN = -(2**63)
_SQLITE_INTEGER_MAX = 2**63 - 1


def _timezone_aware_instant(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware ISO-8601")
    return parsed


def _observed_instant(value: str) -> datetime:
    return _timezone_aware_instant(value, "observed_ts")


def _event_order_key(event: MarketEvent) -> tuple[datetime, int, str]:
    return (_observed_instant(event.observed_ts), event.sequence, _market_event_dedupe_key(event))


def _projection_order_key(event: MarketEvent) -> tuple[int, str]:
    """Provider-local sequence is the live/current authority; receipt time is not."""
    return (event.sequence, _market_event_dedupe_key(event))


def _canonical_json(raw: object, *, _dumps=json.dumps) -> str:
    return _dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_payload(
    event: MarketEvent,
    *,
    _canonical_json_fn=_canonical_json,
    _encode=_encode_market_event,
) -> str:
    return _canonical_json_fn(_encode(event))


def _source_payload_from_raw(raw: object) -> str:
    if not isinstance(raw, dict):
        raise ValueError("stored market event payload must be a JSON object")
    normalized = dict(raw)
    # Local receipt/observation clocks may advance when the same provider
    # sequence is polled again. They are not source-snapshot identity.
    normalized.pop("observed_ts", None)
    normalized.pop("ingest_ts", None)
    return _canonical_json(normalized)


def _source_payload(
    event: MarketEvent,
    *,
    _source_from_raw=_source_payload_from_raw,
    _encode=_encode_market_event,
) -> str:
    return _source_from_raw(_encode(event))


_LIVE_RECEIPT_ISSUE_TOKEN = object()
_LIVE_RECEIPT_CONTEXT = ContextVar("autosport_live_receipt_context", default=None)
# ContextVar values are intentionally copyable. Pair them with a non-propagating
# thread-local lease so a retry hook cannot copy_context() and replay live authority
# after the outer product-owned ingestion call has exited.
_LIVE_RECEIPT_THREAD_STATE = local()


class _LiveReceiptBatch:
    """Immutable internally-issued live snapshot for retry-hook generations."""

    __slots__ = ("_payloads", "_issued_generations", "_issue_token")

    def __init__(
        self,
        events: tuple[MarketEvent, ...],
        *,
        _issue_token: object,
        _expected_issue_token=_LIVE_RECEIPT_ISSUE_TOKEN,
        _canonical_payload_fn=_canonical_payload,
    ) -> None:
        if _issue_token is not _expected_issue_token:
            raise PermissionError("live receipt capability was not internally issued")
        object.__setattr__(
            self,
            "_payloads",
            tuple(_canonical_payload_fn(event) for event in events),
        )
        object.__setattr__(self, "_issued_generations", [])
        object.__setattr__(self, "_issue_token", _issue_token)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("live receipt batch is immutable")

    def __iter__(
        self,
        *,
        _decode=_decode_market_event,
        _loads=json.loads,
    ):
        generation = tuple(_decode(_loads(payload)) for payload in self._payloads)
        self._issued_generations.append(generation)
        return iter(generation)

    def authorizes(
        self,
        events: Iterable[MarketEvent],
        *,
        _expected_issue_token=_LIVE_RECEIPT_ISSUE_TOKEN,
        _market_event_type: type[MarketEvent] = MarketEvent,
        _canonical_payload_fn=_canonical_payload,
    ) -> bool:
        if self._issue_token is not _expected_issue_token:
            return False
        if events is self:
            return True
        if type(events) is not tuple:
            return False
        if any(type(event) is not _market_event_type for event in events):
            return False
        for generation in self._issued_generations:
            if len(events) != len(generation):
                continue
            if not all(candidate is issued for candidate, issued in zip(events, generation)):
                continue
            if tuple(_canonical_payload_fn(event) for event in events) != self._payloads:
                raise ValueError("live receipt retry batch was mutated after issuance")
            return True
        return False


def _typed_equal(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _typed_payload_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if left.keys() != right.keys():
            return False
        return all(_typed_payload_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)):
        if len(left) != len(right):
            return False
        return all(_typed_payload_equal(a, b) for a, b in zip(left, right))
    return left == right


def _reject_duplicate_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"stored market event payload contains duplicate object key: {key}")
        result[key] = value
    return result


def _reject_non_finite_json_constant(value: str) -> object:
    raise ValueError(f"stored market event payload contains non-finite JSON number: {value}")


def _load_history_payload(
    payload_json: str,
    *,
    _loads=json.loads,
) -> dict[str, object]:
    try:
        raw = _loads(
            payload_json,
            object_pairs_hook=_reject_duplicate_object_pairs,
            parse_constant=_reject_non_finite_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("stored market event payload must be valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("stored market event payload must be a JSON object")
    return raw


def _validate_persistable_sequence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("market event sequence must be a non-boolean int")
    if value < _SQLITE_INTEGER_MIN or value > _SQLITE_INTEGER_MAX:
        raise ValueError("market event sequence must fit signed 64-bit SQLite INTEGER")
    return value


def _validate_incoming_event(
    event: MarketEvent,
    *,
    _decode=_decode_market_event,
    _encode=_encode_market_event,
) -> str:
    """Prove an event survives the exact durable JSON/SQLite representation without type drift."""
    _validate_persistable_sequence(event.sequence)
    _observed_instant(event.observed_ts)
    _timezone_aware_instant(event.ingest_ts, "ingest_ts")
    try:
        raw = _encode(event)
        payload = _canonical_json(raw)
        persisted_raw = _load_history_payload(payload)
        if not _typed_payload_equal(raw, persisted_raw):
            raise ValueError("market event JSON representation changes payload types")
        round_tripped = _encode(_decode(persisted_raw))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("market event payload is not canonical") from exc
    if not _typed_payload_equal(persisted_raw, round_tripped):
        raise ValueError("market event payload is not canonical")
    return payload


def _event_from_history_row(
    row: tuple[object, ...],
    *,
    _decode=_decode_market_event,
) -> MarketEvent:
    """Decode one persisted history row while proving redundant identity columns agree."""
    if len(row) != len(_HISTORY_COLUMNS):
        raise ValueError("market event history row has unexpected shape")

    (
        dedupe_key,
        quote_key,
        event_id,
        market_id,
        selection_id,
        decimal_odds,
        observed_ts,
        source_id,
        sequence,
        payload_json,
    ) = row

    if not isinstance(payload_json, str):
        raise ValueError("stored market event payload must be JSON text")
    raw = _load_history_payload(payload_json)
    canonical_raw = _canonical_json(raw)
    if payload_json != canonical_raw:
        raise ValueError("stored market event payload is not canonical JSON text")

    event = _decode(raw)
    if canonical_raw != _canonical_payload(event):
        raise ValueError("stored market event payload is not canonical")

    expected = (
        ("dedupe_key", dedupe_key, _market_event_dedupe_key(event)),
        ("quote_key", quote_key, _market_event_quote_key(event)),
        ("event_id", event_id, event.event_id),
        ("market_id", market_id, event.market_id),
        ("selection_id", selection_id, event.selection_id),
        ("decimal_odds", decimal_odds, str(event.decimal_odds)),
        ("observed_ts", observed_ts, event.observed_ts),
        ("source_id", source_id, event.source_id),
        ("sequence", sequence, event.sequence),
    )
    for field_name, persisted, canonical in expected:
        if not _typed_equal(persisted, canonical):
            raise ValueError(f"market event history row identity mismatch: {field_name}")

    return event


def _event_from_current_payload(
    payload_json: object,
    *,
    _decode=_decode_market_event,
) -> MarketEvent:
    """Decode canonical projection payload independently of repairable redundant columns."""
    if not isinstance(payload_json, str):
        raise ValueError("current quote projection payload must be JSON text")
    raw = _load_history_payload(payload_json)
    try:
        event = _decode(raw)
    except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("current quote projection payload is not canonical") from exc
    if _canonical_json(raw) != _canonical_payload(event):
        raise ValueError("current quote projection payload is not canonical")
    _event_order_key(event)
    return event


def _event_from_current_row(row: tuple[object, ...]) -> MarketEvent:
    """Decode one provider-aware current projection row and prove redundant identity."""
    if len(row) != len(_CURRENT_COLUMNS):
        raise ValueError("current quote projection row has unexpected shape")

    source_id, quote_key, observed_ts, sequence, payload_json = row
    event = _event_from_current_payload(payload_json)

    expected = (
        ("source_id", source_id, event.source_id),
        ("quote_key", quote_key, _market_event_quote_key(event)),
        ("observed_ts", observed_ts, event.observed_ts),
        ("sequence", sequence, event.sequence),
    )
    for field_name, persisted, canonical in expected:
        if not _typed_equal(persisted, canonical):
            raise ValueError(f"current quote projection row identity mismatch: {field_name}")

    return event


def _event_from_legacy_current_row(row: tuple[object, ...]) -> MarketEvent:
    """Decode the exact historical four-column projection during one-way migration."""
    if len(row) != len(_LEGACY_CURRENT_COLUMNS):
        raise ValueError("legacy current quote projection row has unexpected shape")

    quote_key, observed_ts, sequence, payload_json = row
    event = _event_from_current_payload(payload_json)
    expected = (
        ("quote_key", quote_key, _market_event_quote_key(event)),
        ("observed_ts", observed_ts, event.observed_ts),
        ("sequence", sequence, event.sequence),
    )
    for field_name, persisted, canonical in expected:
        if not _typed_equal(persisted, canonical):
            raise ValueError(f"legacy current quote projection row identity mismatch: {field_name}")
    return event


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _canonical_index_terms(connection: sqlite3.Connection, index_name: str) -> tuple[str, ...] | None:
    rows = connection.execute(f"PRAGMA index_xinfo({_quoted_identifier(index_name)})").fetchall()
    key_rows = [row for row in rows if len(row) >= 6 and row[5] == 1]
    terms: list[str] = []
    for row in key_rows:
        _seqno, cid, name, descending, collation, _key = row[:6]
        if not isinstance(cid, int) or cid < 0:
            return None
        if not isinstance(name, str) or descending != 0 or collation != "BINARY":
            return None
        terms.append(name)
    return tuple(terms)


def _semantically_inert_extra_index(connection: sqlite3.Connection, index_name: str) -> bool:
    """Allow only ordinary-column BINARY non-unique indexes as harmless extras."""
    rows = connection.execute(f"PRAGMA index_xinfo({_quoted_identifier(index_name)})").fetchall()
    key_rows = [row for row in rows if len(row) >= 6 and row[5] == 1]
    if not key_rows:
        return False
    for row in key_rows:
        _seqno, cid, name, descending, collation, _key = row[:6]
        if not isinstance(cid, int) or cid < 0:
            return False
        if not isinstance(name, str) or descending != 0 or collation != "BINARY":
            return False
    return True


def _schema_object(connection: sqlite3.Connection, name: str) -> tuple[str, str] | None:
    row = connection.execute(
        "SELECT type, sql FROM sqlite_master WHERE name=?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    object_type, sql = row
    if not isinstance(object_type, str) or not isinstance(sql, str):
        raise ValueError(f"{name} schema object is malformed")
    return object_type, sql


def _validate_table_shape(
    connection: sqlite3.Connection,
    table_name: str,
    expected_xinfo: tuple[tuple[object, ...], ...],
    expected_primary_key: tuple[str, ...],
) -> None:
    schema_object = _schema_object(connection, table_name)
    if schema_object is None or schema_object[0] != "table":
        raise ValueError(f"{table_name} schema is not canonical: expected table")
    table_sql = schema_object[1]
    if _FORBIDDEN_TABLE_SQL.search(table_sql):
        raise ValueError(f"{table_name} schema is not canonical: semantic table constraint")

    rows = connection.execute(f"PRAGMA table_xinfo({_quoted_identifier(table_name)})").fetchall()
    actual_xinfo = tuple(
        (
            int(row[0]),
            row[1],
            str(row[2]).upper(),
            int(row[3]),
            row[4],
            int(row[5]),
            int(row[6]),
        )
        for row in rows
    )
    if actual_xinfo != expected_xinfo:
        raise ValueError(f"{table_name} schema is not canonical: column definition mismatch")

    table_list_rows = connection.execute("PRAGMA table_list").fetchall()
    table_rows = [row for row in table_list_rows if row[0] == "main" and row[1] == table_name]
    if len(table_rows) != 1:
        raise ValueError(f"{table_name} schema is not canonical: table metadata mismatch")
    table_row = table_rows[0]
    if (
        table_row[2] != "table"
        or table_row[3] != len(expected_xinfo)
        or table_row[4] != 0
        or table_row[5] != 0
    ):
        raise ValueError(f"{table_name} schema is not canonical: table mode mismatch")

    if connection.execute(f"PRAGMA foreign_key_list({_quoted_identifier(table_name)})").fetchall():
        raise ValueError(f"{table_name} schema is not canonical: foreign keys are not allowed")

    triggers = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=? ORDER BY name",
        (table_name,),
    ).fetchall()
    if triggers:
        raise ValueError(f"{table_name} schema is not canonical: triggers are not allowed")

    index_rows = connection.execute(f"PRAGMA index_list({_quoted_identifier(table_name)})").fetchall()
    primary_indexes = [row for row in index_rows if len(row) >= 5 and row[3] == "pk"]
    if len(primary_indexes) != 1:
        raise ValueError(f"{table_name} schema is not canonical: primary-key index mismatch")
    primary_index = primary_indexes[0]
    if primary_index[2] != 1 or primary_index[4] != 0:
        raise ValueError(f"{table_name} schema is not canonical: primary-key index mismatch")
    primary_terms = _canonical_index_terms(connection, primary_index[1])
    if primary_terms != expected_primary_key:
        raise ValueError(f"{table_name} schema is not canonical: primary-key definition mismatch")

    for index_row in index_rows:
        if len(index_row) < 5:
            raise ValueError(f"{table_name} schema is not canonical: index metadata mismatch")
        _seq, index_name, unique, origin, partial = index_row[:5]
        if not isinstance(index_name, str):
            raise ValueError(f"{table_name} schema is not canonical: index metadata mismatch")
        if unique and origin != "pk":
            raise ValueError(
                f"{table_name} schema is not canonical: extra UNIQUE index {index_name}"
            )
        is_repairable_canonical_secondary = (
            table_name == "market_events"
            and index_name in _CANONICAL_SECONDARY_INDEXES
        )
        if origin != "pk" and not is_repairable_canonical_secondary:
            if (
                origin != "c"
                or partial != 0
                or not _semantically_inert_extra_index(connection, index_name)
            ):
                raise ValueError(
                    f"{table_name} schema is not canonical: extra non-unique index "
                    f"{index_name} is not semantically inert"
                )


def _validate_canonical_table(connection: sqlite3.Connection, table_name: str) -> None:
    _validate_table_shape(
        connection,
        table_name,
        _EXPECTED_TABLE_XINFO[table_name],
        _EXPECTED_PRIMARY_KEYS[table_name],
    )


def _validate_legacy_current_table(connection: sqlite3.Connection) -> None:
    _validate_table_shape(
        connection,
        "current_quotes",
        _LEGACY_CURRENT_XINFO,
        _LEGACY_CURRENT_PRIMARY_KEYS,
    )


def _validate_live_receipt_rows(
    connection: sqlite3.Connection,
    *,
    _authority: str = _LIVE_RECEIPT_AUTHORITY,
    _decode_history=_event_from_history_row,
    _dedupe_key=_market_event_dedupe_key,
) -> None:
    rows = connection.execute(
        f"""SELECT r.dedupe_key,r.ingest_ts,r.authority,
                   {",".join(f"m.{column}" for column in _HISTORY_COLUMNS)}
            FROM market_event_live_receipts AS r
            LEFT JOIN market_events AS m
            ON m.dedupe_key=r.dedupe_key"""
    ).fetchall()
    for row in rows:
        dedupe_key, ingest_ts, authority = row[:3]
        if (
            not isinstance(dedupe_key, str)
            or not dedupe_key
            or not isinstance(ingest_ts, str)
            or not isinstance(authority, str)
        ):
            raise ValueError("live receipt authority row is malformed")
        if authority != _authority:
            raise ValueError("live receipt authority kind is not canonical")
        _timezone_aware_instant(ingest_ts, "live receipt ingest_ts")
        history_row = row[3:]
        if not history_row or history_row[0] is None:
            raise ValueError("live receipt authority references missing market history")
        event = _decode_history(history_row)
        if _dedupe_key(event) != dedupe_key:
            raise ValueError("live receipt authority dedupe identity does not match market history")
        if event.ingest_ts != ingest_ts:
            raise ValueError("live receipt authority ingest_ts does not match market history")


def _validate_existing_canonical_tables(connection: sqlite3.Connection) -> bool:
    history = _schema_object(connection, "market_events")
    current = _schema_object(connection, "current_quotes")
    live_receipts = _schema_object(connection, "market_event_live_receipts")
    trusted_current = _schema_object(connection, "trusted_live_current_quotes")
    if history is not None:
        _validate_canonical_table(connection, "market_events")
    if live_receipts is not None:
        _validate_canonical_table(connection, "market_event_live_receipts")
        if history is None:
            raise ValueError(
                "market_event_live_receipts cannot exist without authoritative history"
            )
        _validate_live_receipt_rows(connection)
    if trusted_current is not None:
        _validate_canonical_table(connection, "trusted_live_current_quotes")
        if history is None or live_receipts is None:
            raise ValueError(
                "trusted_live_current_quotes cannot exist without live receipt authority"
            )

    legacy_current = False
    if current is not None:
        try:
            _validate_canonical_table(connection, "current_quotes")
        except ValueError as canonical_error:
            try:
                _validate_legacy_current_table(connection)
            except ValueError:
                raise canonical_error
            legacy_current = True

    # current_quotes is a derived projection. It is safe to recreate it from
    # canonical history, but never safe to synthesize missing authoritative
    # history from a projection that cannot prove what was lost.
    if history is None and current is not None:
        raise ValueError(
            "market_events schema is not canonical: authoritative history table is missing"
        )

    if history is not None and current is not None:
        history_has_rows = connection.execute(
            "SELECT 1 FROM market_events LIMIT 1"
        ).fetchone() is not None
        projection_has_rows = connection.execute(
            "SELECT 1 FROM current_quotes LIMIT 1"
        ).fetchone() is not None
        if not history_has_rows and projection_has_rows:
            raise ValueError(
                "market_events history is empty while current_quotes projection is non-empty"
            )
    return legacy_current


def _ensure_canonical_secondary_indexes(connection: sqlite3.Connection) -> None:
    index_rows = connection.execute('PRAGMA index_list("market_events")').fetchall()
    by_name = {row[1]: row for row in index_rows if len(row) >= 5 and isinstance(row[1], str)}

    for index_name, expected_terms in _CANONICAL_SECONDARY_INDEXES.items():
        existing = by_name.get(index_name)
        recreate = existing is None
        if existing is not None:
            _seq, _name, unique, origin, partial = existing[:5]
            if unique:
                raise ValueError(
                    f"market_events schema is not canonical: extra UNIQUE index {index_name}"
                )
            recreate = (
                origin != "c"
                or partial != 0
                or _canonical_index_terms(connection, index_name) != expected_terms
            )
        if recreate:
            if existing is not None:
                connection.execute(f"DROP INDEX {_quoted_identifier(index_name)}")
            columns = ", ".join(_quoted_identifier(column) for column in expected_terms)
            connection.execute(
                f"CREATE INDEX {_quoted_identifier(index_name)} "
                f"ON market_events({columns})"
            )


def _trusted_live_events_from_connection(
    connection: sqlite3.Connection,
    *,
    _authority: str = _LIVE_RECEIPT_AUTHORITY,
    _decode_history=_event_from_history_row,
    _order_key=_event_order_key,
) -> list[MarketEvent]:
    """Read and verify receipt-authoritative history from one locked connection."""
    rows = connection.execute(
        f"""SELECT {",".join(f"m.{column}" for column in _HISTORY_COLUMNS)},
                   r.ingest_ts,r.authority
            FROM market_events AS m
            INNER JOIN market_event_live_receipts AS r
            ON r.dedupe_key=m.dedupe_key"""
    ).fetchall()
    events: list[MarketEvent] = []
    for row in rows:
        event = _decode_history(row[: len(_HISTORY_COLUMNS)])
        receipt_ingest_ts, authority = row[-2:]
        if (
            receipt_ingest_ts != event.ingest_ts
            or authority != _authority
        ):
            raise ValueError("live receipt authority conflicts with market history")
        events.append(event)
    return sorted(events, key=_order_key)


def _trusted_live_current_from_connection(
    connection: sqlite3.Connection,
    *,
    _authority: str = _LIVE_RECEIPT_AUTHORITY,
    _decode_history=_event_from_history_row,
    _quote_key=_market_event_quote_key,
    _dedupe_key=_market_event_dedupe_key,
) -> dict[tuple[str, str], MarketEvent]:
    """Read the bounded derived trusted-current projection and verify every pointer."""

    rows = connection.execute(
        f"""SELECT p.source_id,p.quote_key,p.sequence,p.dedupe_key,
                   {",".join(f"m.{column}" for column in _HISTORY_COLUMNS)},
                   r.ingest_ts,r.authority
            FROM trusted_live_current_quotes AS p
            LEFT JOIN market_events AS m
            ON m.dedupe_key=p.dedupe_key
            LEFT JOIN market_event_live_receipts AS r
            ON r.dedupe_key=p.dedupe_key
            ORDER BY p.source_id,p.quote_key"""
    ).fetchall()

    current: dict[tuple[str, str], MarketEvent] = {}
    prefix_size = 4
    for row in rows:
        source_id, quote_key, sequence, dedupe_key = row[:prefix_size]
        history_row = row[prefix_size : prefix_size + len(_HISTORY_COLUMNS)]
        receipt_ingest_ts, authority = row[-2:]
        if not history_row or history_row[0] is None:
            raise ValueError(
                "trusted live current projection references missing market history"
            )
        if receipt_ingest_ts is None or authority is None:
            raise ValueError(
                "trusted live current projection references missing receipt authority"
            )
        event = _decode_history(history_row)
        expected = (
            ("source_id", source_id, event.source_id),
            ("quote_key", quote_key, _quote_key(event)),
            ("sequence", sequence, event.sequence),
            ("dedupe_key", dedupe_key, _dedupe_key(event)),
        )
        for field_name, persisted, canonical in expected:
            if not _typed_equal(persisted, canonical):
                raise ValueError(
                    "trusted live current projection identity mismatch: "
                    f"{field_name}"
                )
        if receipt_ingest_ts != event.ingest_ts or authority != _authority:
            raise ValueError("live receipt authority conflicts with market history")
        key = (event.source_id, _quote_key(event))
        if key in current:
            raise ValueError("trusted live current projection is ambiguous")
        current[key] = event
    return current

def _has_trusted_live_receipt_from_connection(
    connection: sqlite3.Connection,
    event: MarketEvent,
    *,
    _dedupe_key=_market_event_dedupe_key,
    _canonical_payload_fn=_canonical_payload,
    _authority: str = _LIVE_RECEIPT_AUTHORITY,
    _decode_history=_event_from_history_row,
) -> bool:
    row = connection.execute(
        f"""SELECT {",".join(f"m.{column}" for column in _HISTORY_COLUMNS)},r.ingest_ts,r.authority
            FROM market_events AS m
            INNER JOIN market_event_live_receipts AS r
            ON r.dedupe_key=m.dedupe_key
            WHERE m.dedupe_key=?""",
        (_dedupe_key(event),),
    ).fetchone()
    if row is None:
        return False
    stored = _decode_history(row[: len(_HISTORY_COLUMNS)])
    receipt_ingest_ts, authority = row[-2:]
    if _canonical_payload_fn(stored) != _canonical_payload_fn(event):
        raise ValueError("live receipt authority does not bind the supplied market event")
    if receipt_ingest_ts != stored.ingest_ts or authority != _authority:
        raise ValueError("live receipt authority conflicts with market event")
    return True


class SQLiteMarketStore:
    """Crash-safe append-only normalized market history plus current quote projection.

    One connection remains the canonical durable authority. Cross-thread use is
    explicitly permitted only because every public store operation is serialized by
    ``_connection_lock``; callers must not bypass that boundary for concurrent I/O.
    """

    def __init__(self, path: str | Path = "autosport.db") -> None:
        self.path = Path(path)
        self._connection_lock = RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self._init_schema()
            self._rebuild_current_quotes()
            self._rebuild_trusted_live_current_quotes()
        except Exception:
            self.connection.close()
            raise

    def _create_current_quotes(self) -> None:
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS current_quotes (
                source_id TEXT NOT NULL,
                quote_key TEXT NOT NULL,
                observed_ts TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (source_id, quote_key)
            )"""
        )

    def _migrate_legacy_current_quotes(self) -> None:
        """Migrate only the exact legacy shape after proving every usable projection witness."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            history_by_dedupe: dict[str, MarketEvent] = {}
            rows = self.connection.execute(
                f"SELECT {_HISTORY_COLUMNS_SQL} FROM market_events"
            ).fetchall()
            for row in rows:
                event = _event_from_history_row(row)
                history_by_dedupe[_market_event_dedupe_key(event)] = event

            projection_rows = self.connection.execute(
                f"SELECT {_LEGACY_CURRENT_COLUMNS_SQL} FROM current_quotes"
            ).fetchall()
            for projection_row in projection_rows:
                try:
                    projection_event = _event_from_current_payload(projection_row[3])
                except (KeyError, TypeError, ValueError):
                    continue
                history_event = history_by_dedupe.get(_market_event_dedupe_key(projection_event))
                if history_event is None:
                    raise ValueError(
                        "legacy current_quotes projection event is missing from authoritative history"
                    )
                if _canonical_payload(history_event) != _canonical_payload(projection_event):
                    continue
                try:
                    _event_from_legacy_current_row(projection_row)
                except (KeyError, TypeError, ValueError):
                    continue

            self.connection.execute("DROP TABLE current_quotes")
            self._create_current_quotes()
            _validate_canonical_table(self.connection, "current_quotes")
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _init_schema(self) -> None:
        # Validate any pre-existing tables before creating anything else. Exact
        # four-column current_quotes is the only accepted legacy migration shape.
        legacy_current = _validate_existing_canonical_tables(self.connection)

        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS market_events (
                dedupe_key TEXT PRIMARY KEY,
                quote_key TEXT NOT NULL,
                event_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                selection_id TEXT NOT NULL,
                decimal_odds TEXT NOT NULL,
                observed_ts TEXT NOT NULL,
                source_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            )"""
        )
        if legacy_current:
            self._migrate_legacy_current_quotes()
        else:
            self._create_current_quotes()
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS market_event_live_receipts (
                dedupe_key TEXT PRIMARY KEY,
                ingest_ts TEXT NOT NULL,
                authority TEXT NOT NULL
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS trusted_live_current_quotes (
                source_id TEXT NOT NULL,
                quote_key TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                dedupe_key TEXT NOT NULL,
                PRIMARY KEY (source_id, quote_key)
            )"""
        )

        for table_name in _EXPECTED_TABLE_XINFO:
            _validate_canonical_table(self.connection, table_name)
        _validate_live_receipt_rows(self.connection)
        _ensure_canonical_secondary_indexes(self.connection)
        self.connection.commit()

    def _rebuild_current_quotes(self) -> None:
        """Repair provider-aware current projection from one write-locked history snapshot."""
        latest: dict[tuple[str, str], tuple[tuple[int, str], MarketEvent]] = {}
        history_by_dedupe: dict[str, MarketEvent] = {}
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute(
                f"SELECT {_HISTORY_COLUMNS_SQL} FROM market_events"
            ).fetchall()
            for row in rows:
                event = _event_from_history_row(row)
                history_by_dedupe[_market_event_dedupe_key(event)] = event
                order_key = _projection_order_key(event)
                projection_key = (event.source_id, _market_event_quote_key(event))
                previous = latest.get(projection_key)
                if previous is None or order_key > previous[0]:
                    latest[projection_key] = (order_key, event)

            projection_rows = self.connection.execute(
                f"SELECT {_CURRENT_COLUMNS_SQL} FROM current_quotes"
            ).fetchall()
            for projection_row in projection_rows:
                try:
                    projection_event = _event_from_current_payload(projection_row[4])
                except (KeyError, TypeError, ValueError):
                    continue
                history_event = history_by_dedupe.get(_market_event_dedupe_key(projection_event))
                if history_event is None:
                    raise ValueError(
                        "current_quotes projection event is missing from authoritative history"
                    )
                if _canonical_payload(history_event) != _canonical_payload(projection_event):
                    continue
                try:
                    _event_from_current_row(projection_row)
                except (KeyError, TypeError, ValueError):
                    continue

            self.connection.execute("DELETE FROM current_quotes")
            for projection_key in sorted(latest):
                event = latest[projection_key][1]
                payload = _canonical_payload(event)
                self.connection.execute(
                    """INSERT INTO current_quotes
                       (source_id,quote_key,observed_ts,sequence,payload_json)
                       VALUES (?,?,?,?,?)""",
                    (
                        event.source_id,
                        _market_event_quote_key(event),
                        event.observed_ts,
                        event.sequence,
                        payload,
                    ),
                )
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _rebuild_trusted_live_current_quotes(
        self,
        *,
        _trusted_events=_trusted_live_events_from_connection,
        _quote_key=_market_event_quote_key,
        _dedupe_key=_market_event_dedupe_key,
        _projection_key=_projection_order_key,
    ) -> None:
        """Repair the bounded trusted-current projection from validated receipt history."""

        with self._connection_lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                latest: dict[tuple[str, str], tuple[tuple[int, str], MarketEvent]] = {}
                for event in _trusted_events(self.connection):
                    key = (event.source_id, _quote_key(event))
                    order_key = _projection_key(event)
                    previous = latest.get(key)
                    if previous is None or order_key > previous[0]:
                        latest[key] = (order_key, event)

                self.connection.execute("DELETE FROM trusted_live_current_quotes")
                for key in sorted(latest):
                    event = latest[key][1]
                    self.connection.execute(
                        """INSERT INTO trusted_live_current_quotes
                           (source_id,quote_key,sequence,dedupe_key)
                           VALUES (?,?,?,?)""",
                        (
                            event.source_id,
                            _quote_key(event),
                            event.sequence,
                            _dedupe_key(event),
                        ),
                    )
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def _insert_one(
        self,
        event: MarketEvent,
        *,
        _dedupe_key=_market_event_dedupe_key,
    ) -> bool:
        payload = _validate_incoming_event(event)
        incoming_key = _projection_order_key(event)
        cursor = self.connection.execute(
            """INSERT INTO market_events
               (dedupe_key,quote_key,event_id,market_id,selection_id,decimal_odds,observed_ts,source_id,sequence,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (
                _market_event_dedupe_key(event),
                _market_event_quote_key(event),
                event.event_id,
                event.market_id,
                event.selection_id,
                str(event.decimal_odds),
                event.observed_ts,
                event.source_id,
                event.sequence,
                payload,
            ),
        )
        if cursor.rowcount == 0:
            existing = self.connection.execute(
                f"SELECT {_HISTORY_COLUMNS_SQL} FROM market_events WHERE dedupe_key=?",
                (_dedupe_key(event),),
            ).fetchone()
            if existing is None:
                raise RuntimeError("market event dedupe conflict row disappeared")
            existing_event = _event_from_history_row(existing)
            if _source_payload(existing_event) != _source_payload(event):
                raise ValueError(
                    "conflicting duplicate market event identity: "
                    f"{_market_event_dedupe_key(event)}"
                )
            return False
        previous = self.connection.execute(
            f"""SELECT {_CURRENT_COLUMNS_SQL} FROM current_quotes
                WHERE source_id=? AND quote_key=?""",
            (event.source_id, _market_event_quote_key(event)),
        ).fetchone()
        previous_event = _event_from_current_row(previous) if previous is not None else None
        if previous_event is None or incoming_key > _projection_order_key(previous_event):
            self.connection.execute(
                """INSERT INTO current_quotes
                   (source_id,quote_key,observed_ts,sequence,payload_json)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(source_id,quote_key) DO UPDATE SET
                   observed_ts=excluded.observed_ts,
                   sequence=excluded.sequence,
                   payload_json=excluded.payload_json""",
                (
                    event.source_id,
                    _market_event_quote_key(event),
                    event.observed_ts,
                    event.sequence,
                    payload,
                ),
            )
        return True

    def _insert_live_receipt_authority(
        self,
        event: MarketEvent,
        *,
        _event_type: type[MarketEvent] = MarketEvent,
        _capability_type: type[_LiveReceiptBatch] = _LiveReceiptBatch,
        _expected_issue_token=_LIVE_RECEIPT_ISSUE_TOKEN,
        _live_context=_LIVE_RECEIPT_CONTEXT,
        _thread_state=_LIVE_RECEIPT_THREAD_STATE,
        _canonical_payload_fn=_canonical_payload,
        _dedupe_key=_market_event_dedupe_key,
        _quote_key=_market_event_quote_key,
        _authority: str = _LIVE_RECEIPT_AUTHORITY,
    ) -> None:
        if type(self) is not __class__:
            raise TypeError("live receipt authority requires an exact SQLiteMarketStore")
        if type(event) is not _event_type:
            raise TypeError("live receipt authority requires an exact MarketEvent")
        context = _live_context.get()
        thread_context = getattr(_thread_state, "context", None)
        capability = (
            context[1]
            if context is thread_context
            and type(context) is tuple
            and len(context) == 3
            and context[0] is self
            else None
        )
        if (
            type(capability) is not _capability_type
            or capability._issue_token is not _expected_issue_token
        ):
            raise RuntimeError("live receipt authority requires an internally issued live batch")
        payload = _canonical_payload_fn(event)
        if payload not in capability._payloads:
            raise RuntimeError("live receipt authority event is outside the active live batch")

        dedupe_key = _dedupe_key(event)
        quote_key = _quote_key(event)
        cursor = self.connection.execute(
            """INSERT INTO market_event_live_receipts
               (dedupe_key,ingest_ts,authority)
               VALUES (?,?,?)""",
            (
                dedupe_key,
                event.ingest_ts,
                _authority,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("live receipt authority insert did not persist exactly one row")
        self.connection.execute(
            """INSERT INTO trusted_live_current_quotes
               (source_id,quote_key,sequence,dedupe_key)
               VALUES (?,?,?,?)
               ON CONFLICT(source_id,quote_key) DO UPDATE SET
               sequence=excluded.sequence,
               dedupe_key=excluded.dedupe_key
               WHERE excluded.sequence>trusted_live_current_quotes.sequence
                  OR (
                      excluded.sequence=trusted_live_current_quotes.sequence
                      AND excluded.dedupe_key>trusted_live_current_quotes.dedupe_key
                  )""",
            (
                event.source_id,
                quote_key,
                event.sequence,
                dedupe_key,
            ),
        )

    def _append_live_batch_accepted(
        self,
        events: Iterable[MarketEvent],
        *,
        _market_event_type: type[MarketEvent] = MarketEvent,
        _capability_type: type[_LiveReceiptBatch] = _LiveReceiptBatch,
        _capability_init=_LiveReceiptBatch.__init__,
        _capability_iter=_LiveReceiptBatch.__iter__,
        _issue_token=_LIVE_RECEIPT_ISSUE_TOKEN,
        _live_context=_LIVE_RECEIPT_CONTEXT,
        _thread_state=_LIVE_RECEIPT_THREAD_STATE,
        _object_new=object.__new__,
        _dedupe_key=_market_event_dedupe_key,
        _canonical_payload_fn=_canonical_payload,
        _has_trusted=_has_trusted_live_receipt_from_connection,
    ) -> list[MarketEvent]:
        """Route one exact live-ingestion batch through the canonical write choke point.

        Materialize before arming authority so lazy iterable code runs while no live
        capability exists. Receipt authority is carried by an internal wrapper rather
        than caller-mutable store state. append_batch_accepted remains the canonical
        retry/fault-injection choke point.
        """
        if type(self) is not __class__:
            raise TypeError(
                "live receipt authority requires an exact SQLiteMarketStore"
            )
        materialized = tuple(events)
        if any(type(event) is not _market_event_type for event in materialized):
            raise TypeError("live receipt authority requires exact MarketEvent values")
        capability = _object_new(_capability_type)
        _capability_init(capability, materialized, _issue_token=_issue_token)
        requested_counts: dict[tuple[str, str], int] = {}
        for event in materialized:
            identity = (_dedupe_key(event), _canonical_payload_fn(event))
            requested_counts[identity] = requested_counts.get(identity, 0) + 1

        with self._connection_lock:
            if (
                _live_context.get() is not None
                or getattr(_thread_state, "context", None) is not None
            ):
                raise RuntimeError("nested live receipt authority write is not allowed")
            receipt_keys_before = {
                _dedupe_key(event)
                for event in materialized
                if self.connection.execute(
                    "SELECT 1 FROM market_event_live_receipts WHERE dedupe_key=?",
                    (_dedupe_key(event),),
                ).fetchone()
                is not None
            }
            retry_view = tuple(_capability_iter(capability))
            lease = (self, capability, retry_view)
            _thread_state.context = lease
            context_token = _live_context.set(lease)
            try:
                accepted = self.append_batch_accepted(retry_view)
            finally:
                _live_context.reset(context_token)
                del _thread_state.context

            if type(accepted) is not list:
                raise TypeError("live append must return a list of accepted MarketEvent values")
            remaining = dict(requested_counts)
            for event in accepted:
                if type(event) is not _market_event_type:
                    raise TypeError("live append returned a non-canonical MarketEvent")
                identity = (_dedupe_key(event), _canonical_payload_fn(event))
                available = remaining.get(identity, 0)
                if available <= 0:
                    raise RuntimeError("live append returned an event outside the requested batch")
                remaining[identity] = available - 1
                if identity[0] in receipt_keys_before:
                    raise RuntimeError("live append reported a pre-existing receipt as newly accepted")
                if not _has_trusted(self.connection, event):
                    raise RuntimeError(
                        "live append returned an event without durable receipt authority"
                    )
            return accepted

    def append(self, event: MarketEvent) -> bool:
        with self._connection_lock:
            with self.connection:
                return self._insert_one(event)

    def append_batch_accepted(
        self,
        events: Iterable[MarketEvent],
        *,
        _capability_type: type[_LiveReceiptBatch] = _LiveReceiptBatch,
        _expected_issue_token=_LIVE_RECEIPT_ISSUE_TOKEN,
        _capability_iter=_LiveReceiptBatch.__iter__,
        _live_context=_LIVE_RECEIPT_CONTEXT,
        _thread_state=_LIVE_RECEIPT_THREAD_STATE,
        _insert_one_fn=_insert_one,
        _insert_receipt_fn=_insert_live_receipt_authority,
    ) -> list[MarketEvent]:
        """Insert one normalized batch in one transaction and return newly accepted events."""
        accepted: list[MarketEvent] = []
        with self._connection_lock:
            context = _live_context.get()
            thread_context = getattr(_thread_state, "context", None)
            capability = (
                context[1]
                if context is thread_context
                and type(context) is tuple
                and len(context) == 3
                and context[0] is self
                else None
            )
            retry_view = context[2] if capability is not None else None
            live_receipt_authority = (
                type(capability) is _capability_type
                and capability._issue_token is _expected_issue_token
                and events is retry_view
            )
            # Retry/fault hooks may freely inspect or mutate their view. Once the
            # exact issued view re-enters the canonical append choke point, ignore
            # its mutable values and regenerate a fresh generation from sealed
            # payload snapshots before persistence/receipt publication.
            iteration_events = (
                _capability_iter(capability)
                if live_receipt_authority
                else iter(events)
            )
            with self.connection:
                for event in iteration_events:
                    if _insert_one_fn(self, event):
                        if live_receipt_authority:
                            _insert_receipt_fn(self, event)
                        accepted.append(event)
        return accepted

    def append_many(self, events: Iterable[MarketEvent]) -> int:
        return len(self.append_batch_accepted(events))

    def events(self, event_id: str | None = None) -> list[MarketEvent]:
        with self._connection_lock:
            if event_id is None:
                rows = self.connection.execute(
                    f"SELECT {_HISTORY_COLUMNS_SQL} FROM market_events"
                ).fetchall()
            else:
                rows = self.connection.execute(
                    f"SELECT {_HISTORY_COLUMNS_SQL} FROM market_events WHERE event_id=?",
                    (event_id,),
                ).fetchall()
            events = [_event_from_history_row(row) for row in rows]
        return sorted(events, key=_event_order_key)

    def has_trusted_live_receipt(
        self,
        event: MarketEvent,
        *,
        _market_event_type: type[MarketEvent] = MarketEvent,
        _read=_has_trusted_live_receipt_from_connection,
    ) -> bool:
        if type(event) is not _market_event_type:
            raise TypeError("event must be an exact MarketEvent")
        with self._connection_lock:
            return _read(self.connection, event)

    def trusted_live_events(
        self,
        *,
        _read=_trusted_live_events_from_connection,
    ) -> list[MarketEvent]:
        """Return only history rows whose local receipt instant has product authority."""
        with self._connection_lock:
            return _read(self.connection)

    def trusted_live_current_by_source(
        self,
        *,
        _read=_trusted_live_current_from_connection,
    ) -> dict[tuple[str, str], MarketEvent]:
        """Project latest source-local live state without materializing trusted history."""
        with self._connection_lock:
            return _read(self.connection)

    def current_by_source(self) -> dict[tuple[str, str], MarketEvent]:
        with self._connection_lock:
            rows = self.connection.execute(
                f"SELECT {_CURRENT_COLUMNS_SQL} FROM current_quotes"
            ).fetchall()
            current: dict[tuple[str, str], MarketEvent] = {}
            for row in rows:
                event = _event_from_current_row(row)
                current[(event.source_id, _market_event_quote_key(event))] = event
            return current

    def current(self) -> dict[str, MarketEvent]:
        current: dict[str, MarketEvent] = {}
        for (_source_id, quote_key), event in self.current_by_source().items():
            if quote_key in current:
                raise ValueError(
                    "current quote projection is ambiguous across providers for quote_key: "
                    f"{quote_key}"
                )
            current[quote_key] = event
        return current

    def close(self) -> None:
        with self._connection_lock:
            self.connection.close()

def _seal_live_receipt_authority_call_surfaces() -> None:
    """Hide canonical dependency bindings from callers of receipt-authority APIs."""

    live_append_impl = SQLiteMarketStore._append_live_batch_accepted
    rebuild_trusted_current_impl = SQLiteMarketStore._rebuild_trusted_live_current_quotes
    append_impl = SQLiteMarketStore.append_batch_accepted
    has_receipt_impl = SQLiteMarketStore.has_trusted_live_receipt
    trusted_events_impl = SQLiteMarketStore.trusted_live_events
    trusted_current_impl = SQLiteMarketStore.trusted_live_current_by_source

    def _append_live_batch_accepted(
        self: SQLiteMarketStore,
        events: Iterable[MarketEvent],
    ) -> list[MarketEvent]:
        return live_append_impl(self, events)

    def _insert_live_receipt_authority(
        self: SQLiteMarketStore,
        event: MarketEvent,
    ) -> None:
        raise PermissionError(
            "live receipt authority writes are internal to canonical live ingestion"
        )

    def _rebuild_trusted_live_current_quotes(
        self: SQLiteMarketStore,
    ) -> None:
        return rebuild_trusted_current_impl(self)

    def append_batch_accepted(
        self: SQLiteMarketStore,
        events: Iterable[MarketEvent],
    ) -> list[MarketEvent]:
        return append_impl(self, events)

    def has_trusted_live_receipt(
        self: SQLiteMarketStore,
        event: MarketEvent,
    ) -> bool:
        return has_receipt_impl(self, event)

    def trusted_live_events(self: SQLiteMarketStore) -> list[MarketEvent]:
        return trusted_events_impl(self)

    def trusted_live_current_by_source(
        self: SQLiteMarketStore,
    ) -> dict[tuple[str, str], MarketEvent]:
        return trusted_current_impl(self)

    SQLiteMarketStore._append_live_batch_accepted = _append_live_batch_accepted
    SQLiteMarketStore._insert_live_receipt_authority = _insert_live_receipt_authority
    SQLiteMarketStore._rebuild_trusted_live_current_quotes = _rebuild_trusted_live_current_quotes
    SQLiteMarketStore.append_batch_accepted = append_batch_accepted
    SQLiteMarketStore.has_trusted_live_receipt = has_trusted_live_receipt
    SQLiteMarketStore.trusted_live_events = trusted_live_events
    SQLiteMarketStore.trusted_live_current_by_source = trusted_live_current_by_source


_seal_live_receipt_authority_call_surfaces()
del _seal_live_receipt_authority_call_surfaces
del _LIVE_RECEIPT_ISSUE_TOKEN
del _LIVE_RECEIPT_CONTEXT
del _LIVE_RECEIPT_THREAD_STATE
