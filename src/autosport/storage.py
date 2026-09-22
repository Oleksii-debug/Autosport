from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Final, Iterable

from .domain import MarketEvent
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicAuthorityRecoveryRequiredError,
    MonotonicAuthorityRollbackError,
    MonotonicWorkspaceAuthority,
)


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
    "market_event_commit_order": (
        (0, "dedupe_key", "TEXT", 0, None, 1, 0),
        (1, "append_generation", "INTEGER", 1, None, 0, 0),
    ),
    "market_replay_cutoffs": (
        (0, "cutoff_id", "TEXT", 0, None, 1, 0),
        (1, "as_of", "TEXT", 1, None, 0, 0),
        (2, "max_append_generation", "INTEGER", 1, None, 0, 0),
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
    "market_event_commit_order": ("dedupe_key",),
    "market_replay_cutoffs": ("cutoff_id",),
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
_REPLAY_CUTOFF_DOMAIN = "autosport.market-replay-cutoff.v1"
_REPLAY_CUTOFF_MACHINE_DOMAIN: Final = "data.market-replay-causal-cutoff.v1"
_REPLAY_CUTOFF_MACHINE_KEY_PREFIX: Final = "sqlite-market-store-replay-cutoff:"
_REPLAY_CUTOFF_STATE_SCHEMA: Final = "autosport.market-replay-cutoff.machine-state.v1"
_REPLAY_CUTOFF_CORPUS_SCHEMA: Final = "autosport.market-replay-cutoff.corpus.v1"
_REPLAY_CUTOFF_BINDING_SCHEMA: Final = "autosport.market-replay-cutoff.issuance-binding.v1"
_COMMIT_ORDER_IMMUTABILITY_TRIGGERS: Final = {
    "market_event_commit_order_no_delete": """CREATE TRIGGER market_event_commit_order_no_delete
BEFORE DELETE ON market_event_commit_order
BEGIN
    SELECT RAISE(ABORT, 'market event append-generation rows are immutable');
END""",
    "market_event_commit_order_no_update": """CREATE TRIGGER market_event_commit_order_no_update
BEFORE UPDATE ON market_event_commit_order
BEGIN
    SELECT RAISE(ABORT, 'market event append-generation rows are immutable');
END""",
}
_REPLAY_CUTOFF_IMMUTABILITY_TRIGGERS: Final = {
    "market_replay_cutoffs_no_delete": """CREATE TRIGGER market_replay_cutoffs_no_delete
BEFORE DELETE ON market_replay_cutoffs
BEGIN
    SELECT RAISE(ABORT, 'market replay cutoff rows are immutable');
END""",
    "market_replay_cutoffs_no_update": """CREATE TRIGGER market_replay_cutoffs_no_update
BEFORE UPDATE ON market_replay_cutoffs
BEGIN
    SELECT RAISE(ABORT, 'market replay cutoff rows are immutable');
END""",
}


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


def _canonical_replay_cutoff(value: str) -> str:
    return _timezone_aware_instant(value, "as_of").astimezone(timezone.utc).isoformat()


def _replay_cutoff_id(canonical_as_of: str) -> str:
    payload = f"{_REPLAY_CUTOFF_DOMAIN}\0{canonical_as_of}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _replay_cutoff_state_sha256(
    rows: tuple[tuple[str, str, int], ...],
) -> str | None:
    if not rows:
        return None
    return _canonical_sha256(
        {
            "schema": _REPLAY_CUTOFF_STATE_SCHEMA,
            "cutoffs": [list(row) for row in rows],
        }
    )


def _replay_cutoff_binding_sha256(
    *,
    cutoff_id: str,
    canonical_as_of: str,
    max_append_generation: int,
    corpus_sha256: str,
) -> str:
    return _canonical_sha256(
        {
            "schema": _REPLAY_CUTOFF_BINDING_SCHEMA,
            "cutoff_id": cutoff_id,
            "as_of": canonical_as_of,
            "max_append_generation": max_append_generation,
            "corpus_sha256": corpus_sha256,
        }
    )


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
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='trigger' AND tbl_name=? ORDER BY name",
        (table_name,),
    ).fetchall()
    if table_name == "market_event_commit_order":
        expected_triggers = tuple(
            sorted(_COMMIT_ORDER_IMMUTABILITY_TRIGGERS.items())
        )
        actual_triggers = tuple(
            (name, sql)
            for name, sql in triggers
            if isinstance(name, str) and isinstance(sql, str)
        )
        if actual_triggers != expected_triggers:
            raise ValueError(
                "market_event_commit_order schema is not canonical: "
                "immutable append-generation triggers mismatch"
            )
    elif table_name == "market_replay_cutoffs":
        expected_triggers = tuple(sorted(_REPLAY_CUTOFF_IMMUTABILITY_TRIGGERS.items()))
        actual_triggers = tuple(
            (name, sql)
            for name, sql in triggers
            if isinstance(name, str) and isinstance(sql, str)
        )
        if actual_triggers != expected_triggers:
            raise ValueError(
                "market_replay_cutoffs schema is not canonical: "
                "immutable cutoff triggers mismatch"
            )
    elif triggers:
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

        # The causal companion schema and baseline backfill are one crash-atomic
        # migration. A hard failure cannot leave both tables durable but empty and
        # thereby strand pre-v1 history without commit-generation witnesses.
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            commit_order_state = _schema_object(
                self.connection, "market_event_commit_order"
            )
            replay_cutoff_state = _schema_object(
                self.connection, "market_replay_cutoffs"
            )
            if (commit_order_state is None) != (replay_cutoff_state is None):
                raise ValueError(
                    "causal replay schema is incomplete: "
                    "commit order/cutoff tables disagree"
                )
            initialize_causal_replay = commit_order_state is None
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS market_event_commit_order (
                    dedupe_key TEXT PRIMARY KEY,
                    append_generation INTEGER NOT NULL
                )"""
            )
            self.connection.execute(
                """CREATE TABLE IF NOT EXISTS market_replay_cutoffs (
                    cutoff_id TEXT PRIMARY KEY,
                    as_of TEXT NOT NULL,
                    max_append_generation INTEGER NOT NULL
                )"""
            )
            if initialize_causal_replay:
                for trigger_sql in _COMMIT_ORDER_IMMUTABILITY_TRIGGERS.values():
                    self.connection.execute(trigger_sql)
                for trigger_sql in _REPLAY_CUTOFF_IMMUTABILITY_TRIGGERS.values():
                    self.connection.execute(trigger_sql)
            self.connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_market_event_commit_generation
                   ON market_event_commit_order(append_generation)"""
            )
            if _canonical_index_terms(
                self.connection, "idx_market_event_commit_generation"
            ) != ("append_generation",):
                raise ValueError(
                    "market event append-generation index is not canonical"
                )
            if initialize_causal_replay:
                # Existing pre-v1 history predates product-owned append-generation
                # evidence. Keep it as one coarse generation-0 baseline rather than
                # inventing false relative commit chronology from event timestamps.
                self.connection.execute(
                    """INSERT INTO market_event_commit_order
                       (dedupe_key, append_generation)
                       SELECT dedupe_key, 0 FROM market_events"""
                )

            for table_name in _EXPECTED_TABLE_XINFO:
                _validate_canonical_table(self.connection, table_name)
            _ensure_canonical_secondary_indexes(self.connection)
            self._validate_causal_replay_state()
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _validate_causal_replay_state(self) -> None:
        """Fail closed if durable append-generation/cutoff evidence is inconsistent."""

        # Cutoff immutability is part of the durable authority, not an optional
        # optimization. Revalidate the exact table/trigger contract on every
        # cutoff resolution so a caller with direct SQLite access cannot drop the
        # guards, rewrite an already-issued cutoff, and have the running process
        # silently consume the rolled-back decision corpus.
        _validate_canonical_table(self.connection, "market_event_commit_order")
        _validate_canonical_table(self.connection, "market_replay_cutoffs")

        missing_commit = self.connection.execute(
            """SELECT 1
               FROM market_events AS m
               LEFT JOIN market_event_commit_order AS c
                 ON c.dedupe_key = m.dedupe_key
               WHERE c.dedupe_key IS NULL
               LIMIT 1"""
        ).fetchone()
        orphan_commit = self.connection.execute(
            """SELECT 1
               FROM market_event_commit_order AS c
               LEFT JOIN market_events AS m
                 ON m.dedupe_key = c.dedupe_key
               WHERE m.dedupe_key IS NULL
               LIMIT 1"""
        ).fetchone()
        if missing_commit is not None or orphan_commit is not None:
            raise ValueError(
                "market event append-generation authority does not exactly cover history"
            )

        invalid_generation = self.connection.execute(
            """SELECT 1 FROM market_event_commit_order
               WHERE typeof(append_generation) != 'integer'
                  OR append_generation < 0
               LIMIT 1"""
        ).fetchone()
        if invalid_generation is not None:
            raise ValueError("market event append generation is invalid")

        duplicate_positive = self.connection.execute(
            """SELECT 1
               FROM market_event_commit_order
               WHERE append_generation > 0
               GROUP BY append_generation
               HAVING COUNT(*) != 1
               LIMIT 1"""
        ).fetchone()
        if duplicate_positive is not None:
            raise ValueError("positive market event append generations are not unique")

        positive_shape = self.connection.execute(
            """SELECT COUNT(*), COALESCE(MAX(append_generation), 0)
               FROM market_event_commit_order
               WHERE append_generation > 0"""
        ).fetchone()
        if positive_shape is None:
            raise RuntimeError("cannot verify market event append generations")
        positive_count, max_positive = positive_shape
        if (
            type(positive_count) is not int
            or type(max_positive) is not int
            or positive_count != max_positive
        ):
            raise ValueError("positive market event append generations are not contiguous")

    def _next_append_generation(self) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(append_generation), 0) "
            "FROM market_event_commit_order"
        ).fetchone()
        if row is None or type(row[0]) is not int or row[0] < 0:
            raise ValueError("cannot resolve market event append generation")
        if row[0] >= _SQLITE_INTEGER_MAX:
            raise OverflowError("market event append generation exhausted")
        return row[0] + 1

    def _replay_cutoff_authority(self) -> MonotonicWorkspaceAuthority:
        database_path = self.path.absolute()
        return MonotonicWorkspaceAuthority(
            workspace=database_path.parent,
            domain=_REPLAY_CUTOFF_MACHINE_DOMAIN,
            key=f"{_REPLAY_CUTOFF_MACHINE_KEY_PREFIX}{database_path.name}",
        )

    def _validated_replay_cutoff_rows(self) -> tuple[tuple[str, str, int], ...]:
        latest_row = self.connection.execute(
            """SELECT COALESCE(MAX(append_generation), 0)
               FROM market_event_commit_order"""
        ).fetchone()
        if latest_row is None or type(latest_row[0]) is not int or latest_row[0] < 0:
            raise ValueError("cannot validate causal replay cutoff generation")
        latest_generation = latest_row[0]

        raw_rows = self.connection.execute(
            """SELECT cutoff_id, as_of, max_append_generation
               FROM market_replay_cutoffs
               ORDER BY cutoff_id"""
        ).fetchall()
        rows: list[tuple[str, str, int]] = []
        for cutoff_id, stored_as_of, max_generation in raw_rows:
            if (
                type(cutoff_id) is not str
                or type(stored_as_of) is not str
                or type(max_generation) is not int
                or max_generation < 0
                or max_generation > latest_generation
            ):
                raise ValueError("causal replay cutoff authority is invalid")
            canonical_as_of = _canonical_replay_cutoff(stored_as_of)
            if (
                stored_as_of != canonical_as_of
                or cutoff_id != _replay_cutoff_id(canonical_as_of)
            ):
                raise ValueError("causal replay cutoff authority is invalid")
            rows.append((cutoff_id, canonical_as_of, max_generation))
        return tuple(rows)

    def _frozen_replay_corpus_sha256(self, max_generation: int) -> str:
        if type(max_generation) is not int or max_generation < 0:
            raise ValueError("max_generation must be a non-negative int")
        rows = self.connection.execute(
            """SELECT c.append_generation, m.dedupe_key, m.payload_json
               FROM market_event_commit_order AS c
               JOIN market_events AS m ON m.dedupe_key = c.dedupe_key
               WHERE c.append_generation <= ?
               ORDER BY c.append_generation, m.dedupe_key""",
            (max_generation,),
        ).fetchall()
        encoded_rows: list[list[object]] = []
        for generation, dedupe_key, payload_json in rows:
            if (
                type(generation) is not int
                or generation < 0
                or type(dedupe_key) is not str
                or type(payload_json) is not str
            ):
                raise ValueError("causal replay corpus authority is invalid")
            encoded_rows.append([generation, dedupe_key, payload_json])
        return _canonical_sha256(
            {
                "schema": _REPLAY_CUTOFF_CORPUS_SCHEMA,
                "max_append_generation": max_generation,
                "rows": encoded_rows,
            }
        )

    @staticmethod
    def _recover_replay_cutoff_authority(
        authority: MonotonicWorkspaceAuthority,
        observed_state_sha256: str | None,
    ) -> None:
        try:
            authority.recover(observed_state_sha256=observed_state_sha256)
            return
        except MonotonicAuthorityRecoveryRequiredError:
            history = authority.read_history()
            if not history or history[-1].phase is not AuthorityPhase.PREPARE:
                raise
            pending = history[-1]
            authority.recover(
                observed_state_sha256=observed_state_sha256,
                tx_id=pending.tx_id,
                semantic_binding_sha256=pending.semantic_binding_sha256,
            )

    @staticmethod
    def _require_independent_cutoff_issuance(
        authority: MonotonicWorkspaceAuthority,
        *,
        expected_binding_sha256: str,
    ) -> None:
        matches = tuple(
            record
            for record in authority.read_history()
            if record.phase is AuthorityPhase.COMMIT
            and record.semantic_binding_sha256 == expected_binding_sha256
        )
        if len(matches) != 1:
            raise MonotonicAuthorityRollbackError(
                "causal replay cutoff lacks unique independent product issuance authority"
            )

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
            commit_row = self.connection.execute(
                """SELECT append_generation
                   FROM market_event_commit_order
                   WHERE dedupe_key=?""",
                (event.dedupe_key,),
            ).fetchone()
            if (
                commit_row is None
                or type(commit_row[0]) is not int
                or commit_row[0] < 0
            ):
                raise ValueError(
                    "duplicate market event lacks valid append-generation authority"
                )
            return False

        self.connection.execute(
            """INSERT INTO market_event_commit_order
               (dedupe_key, append_generation)
               VALUES (?, ?)""",
            (event.dedupe_key, self._next_append_generation()),
        )
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
        with self._connection_lock:
            with self.connection:
                return self._insert_one(event)

    def append_batch_accepted(self, events: Iterable[MarketEvent]) -> list[MarketEvent]:
        """Insert one normalized batch in one transaction and return newly accepted events."""
        accepted: list[MarketEvent] = []
        with self._connection_lock:
            with self.connection:
                for event in events:
                    if self._insert_one(event):
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

    def replay_events_at_frozen_cutoff(self, *, as_of: str) -> list[MarketEvent]:
        """Return the exact independently issued durable history cutoff for as_of.

        SQLite remains the canonical event/history store, but a cutoff row is accepted
        only when the existing machine-state MonotonicWorkspaceAuthority proves that
        product issuance. The immutable semantic binding also seals the exact frozen
        event corpus, so coherent same-database DDL rewrites cannot silently change
        membership after the cutoff was issued.
        """

        canonical_as_of = _canonical_replay_cutoff(as_of)
        cutoff_id = _replay_cutoff_id(canonical_as_of)
        with self._connection_lock:
            self._validate_causal_replay_state()
            cutoff_rows = self._validated_replay_cutoff_rows()
            observed_state_sha256 = _replay_cutoff_state_sha256(cutoff_rows)
            authority = self._replay_cutoff_authority()
            self._recover_replay_cutoff_authority(
                authority,
                observed_state_sha256,
            )

            current_row = next(
                (
                    (stored_as_of, max_generation)
                    for stored_cutoff_id, stored_as_of, max_generation in cutoff_rows
                    if stored_cutoff_id == cutoff_id
                ),
                None,
            )

            if current_row is None:
                # Serialize issuance with canonical appends. The external PREPARE is
                # written while the SQLite writer transaction owns the exact prior
                # cutoff-table state; SQLite is then committed before the external
                # COMMIT. A crash in between is recovered from the prepared record and
                # the exact observed cutoff-table digest on the next call.
                prepared: tuple[
                    str,
                    str,
                    str | None,
                    str,
                ] | None = None
                self.connection.execute("BEGIN IMMEDIATE")
                try:
                    self._validate_causal_replay_state()
                    cutoff_rows = self._validated_replay_cutoff_rows()
                    observed_state_sha256 = _replay_cutoff_state_sha256(cutoff_rows)
                    self._recover_replay_cutoff_authority(
                        authority,
                        observed_state_sha256,
                    )
                    current_row = next(
                        (
                            (stored_as_of, max_generation)
                            for stored_cutoff_id, stored_as_of, max_generation in cutoff_rows
                            if stored_cutoff_id == cutoff_id
                        ),
                        None,
                    )
                    if current_row is None:
                        generation_row = self.connection.execute(
                            """SELECT COALESCE(MAX(append_generation), 0)
                               FROM market_event_commit_order"""
                        ).fetchone()
                        if (
                            generation_row is None
                            or type(generation_row[0]) is not int
                            or generation_row[0] < 0
                        ):
                            raise ValueError(
                                "cannot freeze causal replay append generation"
                            )
                        max_generation = generation_row[0]
                        current_row = (canonical_as_of, max_generation)
                        corpus_sha256 = self._frozen_replay_corpus_sha256(
                            max_generation
                        )
                        binding_sha256 = _replay_cutoff_binding_sha256(
                            cutoff_id=cutoff_id,
                            canonical_as_of=canonical_as_of,
                            max_append_generation=max_generation,
                            corpus_sha256=corpus_sha256,
                        )
                        intended_rows = tuple(
                            sorted(
                                (
                                    *cutoff_rows,
                                    (cutoff_id, canonical_as_of, max_generation),
                                ),
                                key=lambda row: row[0],
                            )
                        )
                        intended_state_sha256 = _replay_cutoff_state_sha256(
                            intended_rows
                        )
                        if intended_state_sha256 is None:
                            raise RuntimeError(
                                "non-empty causal replay cutoff state has no digest"
                            )
                        tx_id = f"{cutoff_id[:32]}-{uuid.uuid4().hex}"
                        authority.prepare(
                            tx_id=tx_id,
                            observed_state_sha256=observed_state_sha256,
                            intended_state_sha256=intended_state_sha256,
                            semantic_binding_sha256=binding_sha256,
                        )
                        prepared = (
                            tx_id,
                            binding_sha256,
                            observed_state_sha256,
                            intended_state_sha256,
                        )
                        self.connection.execute(
                            """INSERT INTO market_replay_cutoffs
                               (cutoff_id, as_of, max_append_generation)
                               VALUES (?, ?, ?)""",
                            (cutoff_id, canonical_as_of, max_generation),
                        )
                    self.connection.commit()
                except Exception as exc:
                    self.connection.rollback()
                    if prepared is not None:
                        tx_id, binding_sha256, previous_state_sha256, _ = prepared
                        try:
                            authority.abort(
                                tx_id=tx_id,
                                observed_state_sha256=previous_state_sha256,
                                semantic_binding_sha256=binding_sha256,
                            )
                        except Exception as abort_error:
                            exc.add_note(
                                "independent cutoff-authority PREPARE could not be "
                                f"aborted cleanly: {type(abort_error).__name__}: {abort_error}"
                            )
                    raise

                if prepared is not None:
                    tx_id, binding_sha256, _, intended_state_sha256 = prepared
                    authority.commit(
                        tx_id=tx_id,
                        observed_state_sha256=intended_state_sha256,
                        semantic_binding_sha256=binding_sha256,
                    )

            # Re-read and independently prove the exact durable state after issuance
            # or recovery. A caller-inserted row with no machine-state ancestry fails
            # here even when its SQLite schema/id/generation are individually valid.
            self._validate_causal_replay_state()
            cutoff_rows = self._validated_replay_cutoff_rows()
            observed_state_sha256 = _replay_cutoff_state_sha256(cutoff_rows)
            self._recover_replay_cutoff_authority(
                authority,
                observed_state_sha256,
            )
            current_row = next(
                (
                    (stored_as_of, max_generation)
                    for stored_cutoff_id, stored_as_of, max_generation in cutoff_rows
                    if stored_cutoff_id == cutoff_id
                ),
                None,
            )
            if current_row is None:
                raise RuntimeError("causal replay cutoff issuance disappeared")

            stored_as_of, max_generation = current_row
            if stored_as_of != canonical_as_of:
                raise ValueError("causal replay cutoff authority is invalid")
            corpus_sha256 = self._frozen_replay_corpus_sha256(max_generation)
            expected_binding_sha256 = _replay_cutoff_binding_sha256(
                cutoff_id=cutoff_id,
                canonical_as_of=canonical_as_of,
                max_append_generation=max_generation,
                corpus_sha256=corpus_sha256,
            )
            self._require_independent_cutoff_issuance(
                authority,
                expected_binding_sha256=expected_binding_sha256,
            )

            qualified_columns = ",".join(
                f"m.{column}" for column in _HISTORY_COLUMNS
            )
            rows = self.connection.execute(
                f"""SELECT {qualified_columns}
                    FROM market_events AS m
                    JOIN market_event_commit_order AS c
                      ON c.dedupe_key = m.dedupe_key
                    WHERE c.append_generation <= ?""",
                (max_generation,),
            ).fetchall()

        events = [_event_from_history_row(row) for row in rows]
        return sorted(events, key=_event_order_key)

    def current_by_source(self) -> dict[tuple[str, str], MarketEvent]:
        with self._connection_lock:
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
        with self._connection_lock:
            self.connection.close()