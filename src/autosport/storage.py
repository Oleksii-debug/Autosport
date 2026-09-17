from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
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
    return (_observed_instant(event.observed_ts), event.sequence, event.dedupe_key)


def _projection_order_key(event: MarketEvent) -> tuple[int, str]:
    """Provider-local sequence is the live/current authority; receipt time is not."""
    return (event.sequence, event.dedupe_key)


def _canonical_json(raw: object) -> str:
    return json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_payload(event: MarketEvent) -> str:
    return _canonical_json(event.to_dict())


def _source_payload_from_raw(raw: object) -> str:
    if not isinstance(raw, dict):
        raise ValueError("stored market event payload must be a JSON object")
    normalized = dict(raw)
    # Local receipt/observation clocks may advance when the same provider
    # sequence is polled again. They are not source-snapshot identity.
    normalized.pop("observed_ts", None)
    normalized.pop("ingest_ts", None)
    return _canonical_json(normalized)


def _source_payload(event: MarketEvent) -> str:
    return _source_payload_from_raw(event.to_dict())


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


def _load_history_payload(payload_json: str) -> dict[str, object]:
    try:
        raw = json.loads(
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


def _validate_incoming_event(event: MarketEvent) -> str:
    """Prove an event survives the exact durable JSON/SQLite representation without type drift."""
    _validate_persistable_sequence(event.sequence)
    _observed_instant(event.observed_ts)
    _timezone_aware_instant(event.ingest_ts, "ingest_ts")
    try:
        raw = event.to_dict()
        payload = _canonical_json(raw)
        persisted_raw = _load_history_payload(payload)
        if not _typed_payload_equal(raw, persisted_raw):
            raise ValueError("market event JSON representation changes payload types")
        round_tripped = MarketEvent.from_dict(persisted_raw).to_dict()
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("market event payload is not canonical") from exc
    if not _typed_payload_equal(persisted_raw, round_tripped):
        raise ValueError("market event payload is not canonical")
    return payload


def _event_from_history_row(row: tuple[object, ...]) -> MarketEvent:
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

    event = MarketEvent.from_dict(raw)
    if canonical_raw != _canonical_payload(event):
        raise ValueError("stored market event payload is not canonical")

    expected = (
        ("dedupe_key", dedupe_key, event.dedupe_key),
        ("quote_key", quote_key, event.quote_key),
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


def _event_from_current_payload(payload_json: object) -> MarketEvent:
    """Decode canonical projection payload independently of repairable redundant columns."""
    if not isinstance(payload_json, str):
        raise ValueError("current quote projection payload must be JSON text")
    raw = _load_history_payload(payload_json)
    try:
        event = MarketEvent.from_dict(raw)
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
        ("quote_key", quote_key, event.quote_key),
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
        ("quote_key", quote_key, event.quote_key),
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


def _validate_existing_canonical_tables(connection: sqlite3.Connection) -> bool:
    history = _schema_object(connection, "market_events")
    current = _schema_object(connection, "current_quotes")
    if history is not None:
        _validate_canonical_table(connection, "market_events")

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


class SQLiteMarketStore:
    """Crash-safe append-only normalized market history plus current quote projection."""

    def __init__(self, path: str | Path = "autosport.db") -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self._init_schema()
            self._rebuild_current_quotes()
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
                history_by_dedupe[event.dedupe_key] = event

            projection_rows = self.connection.execute(
                f"SELECT {_LEGACY_CURRENT_COLUMNS_SQL} FROM current_quotes"
            ).fetchall()
            for projection_row in projection_rows:
                try:
                    projection_event = _event_from_current_payload(projection_row[3])
                except (KeyError, TypeError, ValueError):
                    continue
                history_event = history_by_dedupe.get(projection_event.dedupe_key)
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

        for table_name in _EXPECTED_TABLE_XINFO:
            _validate_canonical_table(self.connection, table_name)
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
                history_by_dedupe[event.dedupe_key] = event
                order_key = _projection_order_key(event)
                projection_key = (event.source_id, event.quote_key)
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
                history_event = history_by_dedupe.get(projection_event.dedupe_key)
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
                        event.quote_key,
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

    def _insert_one(self, event: MarketEvent) -> bool:
        payload = _validate_incoming_event(event)
        incoming_key = _projection_order_key(event)
        cursor = self.connection.execute(
            """INSERT INTO market_events
               (dedupe_key,quote_key,event_id,market_id,selection_id,decimal_odds,observed_ts,source_id,sequence,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (
                event.dedupe_key,
                event.quote_key,
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
                (event.dedupe_key,),
            ).fetchone()
            if existing is None:
                raise RuntimeError("market event dedupe conflict row disappeared")
            existing_event = _event_from_history_row(existing)
            if _source_payload(existing_event) != _source_payload(event):
                raise ValueError(
                    "conflicting duplicate market event identity: "
                    f"{event.dedupe_key}"
                )
            return False
        previous = self.connection.execute(
            f"""SELECT {_CURRENT_COLUMNS_SQL} FROM current_quotes
                WHERE source_id=? AND quote_key=?""",
            (event.source_id, event.quote_key),
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
                    event.quote_key,
                    event.observed_ts,
                    event.sequence,
                    payload,
                ),
            )
        return True

    def append(self, event: MarketEvent) -> bool:
        with self.connection:
            return self._insert_one(event)

    def append_batch_accepted(self, events: Iterable[MarketEvent]) -> list[MarketEvent]:
        """Insert one normalized batch in one transaction and return newly accepted events."""
        accepted: list[MarketEvent] = []
        with self.connection:
            for event in events:
                if self._insert_one(event):
                    accepted.append(event)
        return accepted

    def append_many(self, events: Iterable[MarketEvent]) -> int:
        return len(self.append_batch_accepted(events))

    def events(self, event_id: str | None = None) -> list[MarketEvent]:
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

    def current_by_source(self) -> dict[tuple[str, str], MarketEvent]:
        rows = self.connection.execute(
            f"SELECT {_CURRENT_COLUMNS_SQL} FROM current_quotes"
        ).fetchall()
        current: dict[tuple[str, str], MarketEvent] = {}
        for row in rows:
            event = _event_from_current_row(row)
            current[(event.source_id, event.quote_key)] = event
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
        self.connection.close()
