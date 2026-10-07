from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import sqlite3
import stat
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Final, Iterable

from .domain import MarketEvent
from .monotonic_workspace_authority import (
    AuthorityPhase,
    AuthorityRecord,
    MonotonicAuthorityRecoveryRequiredError,
    MonotonicAuthorityRollbackError,
    MonotonicWorkspaceAuthority,
)
from .workspace_lock import WorkspaceEconomicLock


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
_APPEND_MACHINE_DOMAIN: Final = "data.market-event-positive-append.v1"
_APPEND_MACHINE_KEY_PREFIX: Final = "sqlite-market-store-positive-append:"
_APPEND_STATE_SCHEMA: Final = "autosport.market-event-positive-append.chain.v1"
_APPEND_BINDING_SCHEMA: Final = "autosport.market-event-positive-append.binding.v1"
_APPEND_BASELINE_SCHEMA: Final = "autosport.market-event-generation-zero-baseline.v1"
_APPEND_BASELINE_BINDING_SCHEMA: Final = "autosport.market-event-generation-zero-baseline.binding.v1"
_APPEND_BASELINE_TX_RE: Final = re.compile(r"^baseline-(?P<nonce>[0-9a-f]{32})$")
_APPEND_TX_RE: Final = re.compile(
    r"^append-(?P<start>[1-9][0-9]*)-(?P<end>[1-9][0-9]*)-(?P<nonce>[0-9a-f]{32})$"
)
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
    if type(value) is not str:
        raise ValueError(f"{field_name} must be an exact ISO-8601 string")
    # datetime.fromisoformat() silently truncates fractional-second/offset
    # precision beyond microseconds. Reject only discarded non-zero digits so
    # D+submicrosecond evidence can never be rounded backward onto cutoff D.
    for match in re.finditer(r"[.,]([0-9]+)", value):
        fractional_digits = match.group(1)
        if len(fractional_digits) > 6 and any(
            digit != "0" for digit in fractional_digits[6:]
        ):
            raise ValueError(
                f"{field_name} precision finer than microseconds is unsupported"
            )
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


def _database_authority_key(path: Path) -> str:
    """Return one machine-authority key for filesystem-equivalent database names."""

    name = path.name
    if os.name == "nt":
        # A new database can be opened concurrently through Win32 case/trailing-dot
        # aliases before Path.resolve() has an existing target from which to recover
        # canonical spelling. Those aliases still identify one file, so they must not
        # mint independent append/cutoff authority namespaces.
        name = ntpath.normcase(name).rstrip(" .")
    if not name:
        raise ValueError("market database pathname has no canonical authority name")
    return name


def _replay_cutoff_id(canonical_as_of: str) -> str:
    payload = f"{_REPLAY_CUTOFF_DOMAIN}\0{canonical_as_of}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _append_baseline_state_sha256(
    entries: tuple[tuple[str, str], ...],
) -> str:
    return _canonical_sha256(
        {
            "schema": _APPEND_BASELINE_SCHEMA,
            "entries": [list(entry) for entry in entries],
        }
    )


def _append_baseline_binding_sha256(state_sha256: str) -> str:
    return _canonical_sha256(
        {
            "schema": _APPEND_BASELINE_BINDING_SCHEMA,
            "baseline_state_sha256": state_sha256,
        }
    )


def _append_state_step_sha256(
    previous_state_sha256: str | None,
    *,
    append_generation: int,
    dedupe_key: str,
    payload_json: str,
) -> str:
    if (
        type(append_generation) is not int
        or append_generation <= 0
        or type(dedupe_key) is not str
        or not dedupe_key
        or type(payload_json) is not str
    ):
        raise ValueError("positive market append authority entry is invalid")
    return _canonical_sha256(
        {
            "schema": _APPEND_STATE_SCHEMA,
            "previous_state_sha256": previous_state_sha256,
            "append_generation": append_generation,
            "dedupe_key": dedupe_key,
            "payload_json": payload_json,
        }
    )


def _append_binding_sha256(
    *,
    previous_state_sha256: str | None,
    intended_state_sha256: str,
    entries: tuple[tuple[int, str, str], ...],
) -> str:
    return _canonical_sha256(
        {
            "schema": _APPEND_BINDING_SCHEMA,
            "previous_state_sha256": previous_state_sha256,
            "intended_state_sha256": intended_state_sha256,
            "entries": [list(entry) for entry in entries],
        }
    )


def _replay_cutoff_state_sha256(
    rows: tuple[tuple[str, str, int], ...],
    *,
    sealed_corpus_sha256: str | None,
) -> str | None:
    if not rows:
        if sealed_corpus_sha256 is not None:
            raise ValueError("empty replay cutoff state cannot seal a corpus")
        return None
    if type(sealed_corpus_sha256) is not str or len(sealed_corpus_sha256) != 64:
        raise ValueError("replay cutoff state requires a canonical sealed corpus digest")
    return _canonical_sha256(
        {
            "schema": _REPLAY_CUTOFF_STATE_SCHEMA,
            "cutoffs": [list(row) for row in rows],
            "sealed_corpus_sha256": sealed_corpus_sha256,
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


def _stream_semantic_identity(event: MarketEvent) -> tuple[str, str | None]:
    """Stable market-rule identity for one provider/source quote stream."""
    return (event.market_type.value, event.market_semantics_id)


def _assert_stream_semantic_identity(
    expected: tuple[str, str | None],
    event: MarketEvent,
) -> None:
    if _stream_semantic_identity(event) != expected:
        raise ValueError(
            "market quote stream semantic identity changed: "
            f"{event.source_id}|{event.quote_key}"
        )


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
    _observed_instant(event.observed_ts)
    _timezone_aware_instant(event.ingest_ts, "ingest_ts")

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
    # SQLite resolves TEMP objects before main for unqualified names. Reject both
    # exact-name shadows (TEMP TABLE/VIEW) and TEMP triggers/indexes attached to a
    # canonical table before any PRAGMA or data statement can resolve through the
    # wrong schema.
    temp_objects = connection.execute(
        """SELECT type, name, tbl_name
           FROM sqlite_temp_master
           WHERE name=? OR tbl_name=?
           ORDER BY type, name""",
        (table_name, table_name),
    ).fetchall()
    if temp_objects:
        raise ValueError(
            f"{table_name} schema is not canonical: temporary schema objects are not allowed"
        )

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
        # Freeze one filesystem-canonical database pathname before SQLite or any
        # independent authority derives identity from it. In particular, a file
        # symlink alias must not derive a second independent authority key for the
        # same durable database.
        self.path = Path(path).resolve(strict=False)
        self._connection_lock = RLock()

        # Ensure even a brand-new database has an inode that can be witnessed both
        # before and after sqlite3.connect(). Without this pre/post witness, a
        # pathname replacement in the connect -> first-lstat window could leave the
        # SQLite connection bound to one inode while machine authority trusts another.
        try:
            file_descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            pass
        else:
            os.close(file_descriptor)

        pre_open_identity = self._current_database_path_identity()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        try:
            opened_identity = self._current_database_path_identity()
            if not os.path.samestat(pre_open_identity, opened_identity):
                raise ValueError(
                    "market database pathname changed while opening database"
                )
            self._database_identity = opened_identity
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self._init_schema()
            append_authority = self._market_append_authority()
            # Baseline PREPARE/COMMIT is part of the same product-owned append
            # authority lifecycle as positive generations. Keep it under the sibling
            # issuance lock so a concurrent constructor cannot mistake a live
            # generation-zero PREPARE for abandoned crash state.
            with self._market_append_issuance_lock(append_authority):
                self._ensure_market_append_baseline_authority()
                self._rebuild_current_quotes(
                    append_authority=append_authority,
                )
        except BaseException:
            self.connection.close()
            raise

    def _current_database_path_identity(self) -> os.stat_result:
        try:
            metadata = self.path.lstat()
        except OSError as exc:
            raise ValueError(
                "market database pathname is missing or inaccessible"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(
                "market database pathname must be a single-link regular file"
            )
        return metadata

    def _require_database_path_identity(self) -> None:
        current = self._current_database_path_identity()
        if not os.path.samestat(self._database_identity, current):
            raise ValueError(
                "market database pathname no longer identifies the opened database"
            )

    def _commit_stable_database_path(self) -> None:
        """Commit only while the canonical pathname still identifies this database."""
        self._require_database_path_identity()
        self.connection.commit()
        self._require_database_path_identity()

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
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self._commit_stable_database_path()

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
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self._commit_stable_database_path()

    def _validate_causal_replay_state(self) -> None:
        """Fail closed if durable append-generation/cutoff evidence is inconsistent."""

        # Cutoff immutability is part of the durable authority, not an optional
        # optimization. Revalidate every table read below so SQLite TEMP namespace
        # shadows cannot redirect causal validation away from canonical main history.
        _validate_canonical_table(self.connection, "market_events")
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

    def _market_append_authority(self) -> MonotonicWorkspaceAuthority:
        self._require_database_path_identity()
        database_path = self.path
        return MonotonicWorkspaceAuthority(
            workspace=database_path.parent,
            domain=_APPEND_MACHINE_DOMAIN,
            key=f"{_APPEND_MACHINE_KEY_PREFIX}{_database_authority_key(database_path)}",
        )

    @staticmethod
    def _market_append_issuance_lock(
        authority: MonotonicWorkspaceAuthority,
    ) -> WorkspaceEconomicLock:
        # Serialize product append PREPARE -> SQLite COMMIT -> machine COMMIT.
        # Direct SQLite writers do not participate in this lock, so cutoff issuance
        # still independently recomputes and proves the complete positive chain.
        return WorkspaceEconomicLock(
            authority.journal_dir / "market-positive-append-issuance"
        )

    def _validated_generation_zero_entries(
        self,
    ) -> tuple[tuple[str, str], ...]:
        qualified_columns = ",".join(
            f"m.{column}" for column in _HISTORY_COLUMNS
        )
        rows = self.connection.execute(
            f"""SELECT {qualified_columns}
                FROM market_event_commit_order AS c
                JOIN market_events AS m ON m.dedupe_key = c.dedupe_key
                WHERE c.append_generation = 0
                ORDER BY m.dedupe_key"""
        ).fetchall()
        entries: list[tuple[str, str]] = []
        for row in rows:
            history_row = tuple(row)
            _event_from_history_row(history_row)
            dedupe_key = history_row[0]
            payload_json = history_row[-1]
            if type(dedupe_key) is not str or type(payload_json) is not str:
                raise ValueError("generation-zero market baseline row is invalid")
            entries.append((dedupe_key, payload_json))
        return tuple(entries)

    def _generation_zero_baseline_state_sha256(self) -> str:
        return _append_baseline_state_sha256(
            self._validated_generation_zero_entries()
        )

    def _ensure_market_append_baseline_authority(self) -> None:
        """Seal baseline membership without claiming historical receipt chronology."""

        authority = self._market_append_authority()
        observed_state_sha256 = self._generation_zero_baseline_state_sha256()
        history = authority.read_history()

        committed = tuple(
            record for record in history if record.phase is AuthorityPhase.COMMIT
        )
        if not committed:
            if history:
                pending = history[-1]
                expected_binding_sha256 = _append_baseline_binding_sha256(
                    observed_state_sha256
                )
                if (
                    pending.phase is not AuthorityPhase.PREPARE
                    or _APPEND_BASELINE_TX_RE.fullmatch(pending.tx_id) is None
                    or pending.previous_committed_state_sha256 is not None
                    or pending.intended_state_sha256 != observed_state_sha256
                    or pending.semantic_binding_sha256 != expected_binding_sha256
                ):
                    raise MonotonicAuthorityRollbackError(
                        "market append authority has noncanonical generation-zero PREPARE"
                    )
                try:
                    authority.recover(
                        observed_state_sha256=observed_state_sha256,
                    )
                except MonotonicAuthorityRecoveryRequiredError:
                    authority.recover(
                        observed_state_sha256=observed_state_sha256,
                        tx_id=pending.tx_id,
                        semantic_binding_sha256=pending.semantic_binding_sha256,
                    )
                history = authority.read_history()
                committed = tuple(
                    record
                    for record in history
                    if record.phase is AuthorityPhase.COMMIT
                )

            if not committed:
                binding_sha256 = _append_baseline_binding_sha256(
                    observed_state_sha256
                )
                tx_id = f"baseline-{uuid.uuid4().hex}"
                authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=None,
                    intended_state_sha256=observed_state_sha256,
                    semantic_binding_sha256=binding_sha256,
                )
                authority.recover(
                    observed_state_sha256=observed_state_sha256,
                    tx_id=tx_id,
                    semantic_binding_sha256=binding_sha256,
                )
                history = authority.read_history()
                committed = tuple(
                    record
                    for record in history
                    if record.phase is AuthorityPhase.COMMIT
                )

        first_commit = committed[0]
        if (
            _APPEND_BASELINE_TX_RE.fullmatch(first_commit.tx_id) is None
            or first_commit.previous_committed_state_sha256 is not None
            or first_commit.intended_state_sha256 != observed_state_sha256
            or first_commit.semantic_binding_sha256
            != _append_baseline_binding_sha256(observed_state_sha256)
        ):
            raise MonotonicAuthorityRollbackError(
                "generation-zero market baseline is missing, changed, or unproven"
            )

    @staticmethod
    def _append_authority_committed_tip(
        history: tuple[AuthorityRecord, ...],
    ) -> tuple[int, str]:
        commits = tuple(
            record for record in history if record.phase is AuthorityPhase.COMMIT
        )
        if not commits:
            raise MonotonicAuthorityRollbackError(
                "market append authority lacks a committed generation-zero baseline"
            )
        baseline = commits[0]
        if (
            _APPEND_BASELINE_TX_RE.fullmatch(baseline.tx_id) is None
            or baseline.previous_committed_state_sha256 is not None
        ):
            raise MonotonicAuthorityRollbackError(
                "market append authority baseline history is invalid"
            )

        expected_start = 1
        committed_head = 0
        committed_state_sha256 = baseline.intended_state_sha256
        for record in commits[1:]:
            match = _APPEND_TX_RE.fullmatch(record.tx_id)
            if match is None:
                raise MonotonicAuthorityRollbackError(
                    "positive market append authority history has invalid transaction identity"
                )
            start = int(match.group("start"))
            end = int(match.group("end"))
            if start != expected_start or end < start:
                raise MonotonicAuthorityRollbackError(
                    "positive market append authority history is non-contiguous"
                )
            if record.previous_committed_state_sha256 != committed_state_sha256:
                raise MonotonicAuthorityRollbackError(
                    "positive market append authority history has inconsistent state ancestry"
                )
            committed_head = end
            committed_state_sha256 = record.intended_state_sha256
            expected_start = end + 1
        return committed_head, committed_state_sha256

    @staticmethod
    def _require_canonical_append_authority_bindings(
        history: tuple[AuthorityRecord, ...],
        entries: tuple[tuple[int, str, str], ...],
        *,
        baseline_state_sha256: str,
    ) -> None:
        """Prove every committed append transition carries the canonical product binding."""

        commits = tuple(
            record for record in history if record.phase is AuthorityPhase.COMMIT
        )
        if not commits:
            raise MonotonicAuthorityRollbackError(
                "market append authority lacks a committed generation-zero baseline"
            )

        baseline = commits[0]
        expected_baseline_binding = _append_baseline_binding_sha256(
            baseline_state_sha256
        )
        if (
            _APPEND_BASELINE_TX_RE.fullmatch(baseline.tx_id) is None
            or baseline.previous_committed_state_sha256 is not None
            or baseline.intended_state_sha256 != baseline_state_sha256
            or baseline.semantic_binding_sha256 != expected_baseline_binding
        ):
            raise MonotonicAuthorityRollbackError(
                "market append authority baseline semantic binding is invalid"
            )

        entry_index = 0
        previous_state_sha256 = baseline_state_sha256
        expected_start = 1
        for record in commits[1:]:
            match = _APPEND_TX_RE.fullmatch(record.tx_id)
            if match is None:
                raise MonotonicAuthorityRollbackError(
                    "positive market append authority history has invalid transaction identity"
                )
            start = int(match.group("start"))
            end = int(match.group("end"))
            if start != expected_start or end < start:
                raise MonotonicAuthorityRollbackError(
                    "positive market append authority history is non-contiguous"
                )

            count = end - start + 1
            transition_entries = entries[entry_index : entry_index + count]
            if (
                len(transition_entries) != count
                or tuple(entry[0] for entry in transition_entries)
                != tuple(range(start, end + 1))
            ):
                raise MonotonicAuthorityRollbackError(
                    "positive market append authority transition does not match durable entries"
                )

            intended_state_sha256 = previous_state_sha256
            for generation, dedupe_key, payload_json in transition_entries:
                intended_state_sha256 = _append_state_step_sha256(
                    intended_state_sha256,
                    append_generation=generation,
                    dedupe_key=dedupe_key,
                    payload_json=payload_json,
                )

            expected_binding_sha256 = _append_binding_sha256(
                previous_state_sha256=previous_state_sha256,
                intended_state_sha256=intended_state_sha256,
                entries=transition_entries,
            )
            if (
                record.previous_committed_state_sha256 != previous_state_sha256
                or record.intended_state_sha256 != intended_state_sha256
                or record.semantic_binding_sha256 != expected_binding_sha256
            ):
                raise MonotonicAuthorityRollbackError(
                    "positive market append authority semantic binding is invalid"
                )

            entry_index += count
            expected_start = end + 1
            previous_state_sha256 = intended_state_sha256

        if entry_index != len(entries):
            raise MonotonicAuthorityRollbackError(
                "positive market append authority does not cover durable entries"
            )

    @staticmethod
    def _require_canonical_pending_append_binding(
        pending: AuthorityRecord,
        entries: tuple[tuple[int, str, str], ...],
        *,
        committed_head: int,
        committed_state_sha256: str,
    ) -> None:
        """Reject a non-product PREPARE before recovery can turn it into COMMIT."""

        match = _APPEND_TX_RE.fullmatch(pending.tx_id)
        if (
            pending.phase is not AuthorityPhase.PREPARE
            or match is None
            or pending.previous_committed_state_sha256 != committed_state_sha256
        ):
            raise MonotonicAuthorityRollbackError(
                "positive market append authority has noncanonical pending transition"
            )

        start = int(match.group("start"))
        end = int(match.group("end"))
        transition_entries = entries[committed_head:]
        if (
            start != committed_head + 1
            or end < start
            or len(transition_entries) != end - start + 1
            or tuple(entry[0] for entry in transition_entries)
            != tuple(range(start, end + 1))
        ):
            raise MonotonicAuthorityRollbackError(
                "positive market append PREPARE does not match durable entries"
            )

        intended_state_sha256 = committed_state_sha256
        for generation, dedupe_key, payload_json in transition_entries:
            intended_state_sha256 = _append_state_step_sha256(
                intended_state_sha256,
                append_generation=generation,
                dedupe_key=dedupe_key,
                payload_json=payload_json,
            )
        expected_binding_sha256 = _append_binding_sha256(
            previous_state_sha256=committed_state_sha256,
            intended_state_sha256=intended_state_sha256,
            entries=transition_entries,
        )
        if (
            pending.intended_state_sha256 != intended_state_sha256
            or pending.semantic_binding_sha256 != expected_binding_sha256
        ):
            raise MonotonicAuthorityRollbackError(
                "positive market append PREPARE semantic binding is invalid"
            )

    def _positive_append_generation_head(self) -> int:
        row = self.connection.execute(
            """SELECT COUNT(*), COALESCE(MAX(append_generation), 0)
               FROM market_event_commit_order
               WHERE append_generation > 0"""
        ).fetchone()
        if (
            row is None
            or type(row[0]) is not int
            or type(row[1]) is not int
            or row[0] != row[1]
            or row[1] < 0
        ):
            raise ValueError("positive market event append generations are not contiguous")
        return row[1]

    def _validated_positive_append_entries(
        self,
        *,
        max_generation: int | None = None,
    ) -> tuple[tuple[int, str, str], ...]:
        qualified_columns = ",".join(
            f"m.{column}" for column in _HISTORY_COLUMNS
        )
        if (
            max_generation is not None
            and (type(max_generation) is not int or max_generation < 0)
        ):
            raise ValueError("max_generation must be a non-negative int")
        if max_generation is None:
            rows = self.connection.execute(
                f"""SELECT c.append_generation, {qualified_columns}
                    FROM market_event_commit_order AS c
                    JOIN market_events AS m ON m.dedupe_key = c.dedupe_key
                    WHERE c.append_generation > 0
                    ORDER BY c.append_generation"""
            ).fetchall()
        else:
            rows = self.connection.execute(
                f"""SELECT c.append_generation, {qualified_columns}
                    FROM market_event_commit_order AS c
                    JOIN market_events AS m ON m.dedupe_key = c.dedupe_key
                    WHERE c.append_generation > 0
                      AND c.append_generation <= ?
                    ORDER BY c.append_generation""",
                (max_generation,),
            ).fetchall()
        entries: list[tuple[int, str, str]] = []
        expected_generation = 1
        for row in rows:
            if len(row) != len(_HISTORY_COLUMNS) + 1:
                raise ValueError("positive market append authority row has invalid shape")
            generation = row[0]
            history_row = tuple(row[1:])
            if type(generation) is not int or generation != expected_generation:
                raise ValueError("positive market append authority is non-contiguous")
            _event_from_history_row(history_row)
            dedupe_key = history_row[0]
            payload_json = history_row[-1]
            if type(dedupe_key) is not str or type(payload_json) is not str:
                raise ValueError("positive market append authority row is invalid")
            entries.append((generation, dedupe_key, payload_json))
            expected_generation += 1
        return tuple(entries)

    @staticmethod
    def _append_state_from_entries(
        entries: tuple[tuple[int, str, str], ...],
        *,
        baseline_state_sha256: str,
    ) -> str:
        state_sha256 = baseline_state_sha256
        expected_generation = 1
        for generation, dedupe_key, payload_json in entries:
            if generation != expected_generation:
                raise ValueError("positive market append authority is non-contiguous")
            state_sha256 = _append_state_step_sha256(
                state_sha256,
                append_generation=generation,
                dedupe_key=dedupe_key,
                payload_json=payload_json,
            )
            expected_generation += 1
        return state_sha256

    def _recover_positive_append_authority(
        self,
        authority: MonotonicWorkspaceAuthority,
    ) -> tuple[int, str]:
        history = authority.read_history()
        if history and history[-1].phase is AuthorityPhase.PREPARE:
            pending = history[-1]
            entries = self._validated_positive_append_entries()
            baseline_state_sha256 = self._generation_zero_baseline_state_sha256()
            committed_head, committed_state_sha256 = (
                self._append_authority_committed_tip(history)
            )
            if len(entries) < committed_head:
                raise MonotonicAuthorityRollbackError(
                    "positive market append chronology is missing durable committed entries"
                )
            self._require_canonical_append_authority_bindings(
                history,
                entries[:committed_head],
                baseline_state_sha256=baseline_state_sha256,
            )
            observed_state_sha256 = self._append_state_from_entries(
                entries,
                baseline_state_sha256=baseline_state_sha256,
            )
            try:
                authority.recover(observed_state_sha256=observed_state_sha256)
            except MonotonicAuthorityRecoveryRequiredError:
                self._require_canonical_pending_append_binding(
                    pending,
                    entries,
                    committed_head=committed_head,
                    committed_state_sha256=committed_state_sha256,
                )
                authority.recover(
                    observed_state_sha256=observed_state_sha256,
                    tx_id=pending.tx_id,
                    semantic_binding_sha256=pending.semantic_binding_sha256,
                )
            history = authority.read_history()
        committed_head, committed_state_sha256 = (
            self._append_authority_committed_tip(history)
        )

        # A matching numeric head is not sufficient authority.  A direct SQLite
        # writer can coherently rewrite an already-issued positive MarketEvent while
        # preserving every generation number.  Recompute the complete product-owned
        # chain before any caller is allowed to extend or rely on that authority.
        entries = self._validated_positive_append_entries()
        baseline_state_sha256 = self._generation_zero_baseline_state_sha256()
        observed_state_sha256 = self._append_state_from_entries(
            entries,
            baseline_state_sha256=baseline_state_sha256,
        )
        self._require_canonical_append_authority_bindings(
            history,
            entries,
            baseline_state_sha256=baseline_state_sha256,
        )
        observed_head = entries[-1][0] if entries else 0
        if (
            observed_head != committed_head
            or observed_state_sha256 != committed_state_sha256
        ):
            raise MonotonicAuthorityRollbackError(
                "positive market append chronology is missing, forged, or unproven"
            )
        return committed_head, committed_state_sha256

    def _require_product_issued_positive_history(
        self,
        authority: MonotonicWorkspaceAuthority,
    ) -> None:
        # Cutoff issuance runs this while holding BEGIN IMMEDIATE, so an
        # uncooperating direct SQLite writer cannot change the corpus between this
        # proof and cutoff publication.  _recover_positive_append_authority performs
        # the full baseline + positive-chain digest proof, not merely a head check.
        self._recover_positive_append_authority(authority)

    def _require_committed_append_authority_through(
        self,
        authority: MonotonicWorkspaceAuthority,
        max_generation: int,
    ) -> None:
        """Prove a frozen cutoff is an exact committed append transition prefix.

        This deliberately does not recover a pending append. Existing cutoffs must
        remain readable while a newer live writer owns PREPARE, but they may never
        rely on a SQLite generation that lacks an independently committed product
        append transition. A cutoff inside one atomic multi-event append batch is
        likewise invalid: only the batch's committed end generation was ever a
        product-issued durable state.
        """

        if type(max_generation) is not int or max_generation < 0:
            raise ValueError("max_generation must be a non-negative int")
        history = authority.read_history()
        commits = tuple(
            record for record in history if record.phase is AuthorityPhase.COMMIT
        )
        if not commits:
            raise MonotonicAuthorityRollbackError(
                "market append authority lacks a committed generation-zero baseline"
            )

        prefix_commits: list[AuthorityRecord] = [commits[0]]
        covered_generation = 0
        expected_start = 1
        for record in commits[1:]:
            if covered_generation == max_generation:
                break
            match = _APPEND_TX_RE.fullmatch(record.tx_id)
            if match is None:
                raise MonotonicAuthorityRollbackError(
                    "positive market append authority history has invalid transaction identity"
                )
            start = int(match.group("start"))
            end = int(match.group("end"))
            if start != expected_start or end < start:
                raise MonotonicAuthorityRollbackError(
                    "positive market append authority history is non-contiguous"
                )
            if end > max_generation:
                raise MonotonicAuthorityRollbackError(
                    "causal replay cutoff is not an exact committed append transition boundary"
                )
            prefix_commits.append(record)
            covered_generation = end
            expected_start = end + 1

        if covered_generation != max_generation:
            raise MonotonicAuthorityRollbackError(
                "causal replay cutoff exceeds independently committed append authority"
            )

        entries = self._validated_positive_append_entries(
            max_generation=max_generation
        )
        if len(entries) != max_generation:
            raise MonotonicAuthorityRollbackError(
                "committed append authority is missing durable market history"
            )
        baseline_state_sha256 = self._generation_zero_baseline_state_sha256()
        self._require_canonical_append_authority_bindings(
            tuple(prefix_commits),
            entries,
            baseline_state_sha256=baseline_state_sha256,
        )

    def _replay_cutoff_authority(self) -> MonotonicWorkspaceAuthority:
        self._require_database_path_identity()
        database_path = self.path
        return MonotonicWorkspaceAuthority(
            workspace=database_path.parent,
            domain=_REPLAY_CUTOFF_MACHINE_DOMAIN,
            key=f"{_REPLAY_CUTOFF_MACHINE_KEY_PREFIX}{_database_authority_key(database_path)}",
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

    def _replay_cutoff_authority_state_sha256(
        self,
        rows: tuple[tuple[str, str, int], ...],
    ) -> str | None:
        if not rows:
            return _replay_cutoff_state_sha256(
                rows,
                sealed_corpus_sha256=None,
            )
        sealed_generation = max(row[2] for row in rows)
        return _replay_cutoff_state_sha256(
            rows,
            sealed_corpus_sha256=self._frozen_replay_corpus_sha256(
                sealed_generation
            ),
        )

    @staticmethod
    def _replay_cutoff_issuance_lock(
        authority: MonotonicWorkspaceAuthority,
    ) -> WorkspaceEconomicLock:
        # Keep one crash-releasing resolver transaction lock outside market.db.
        # MonotonicWorkspaceAuthority owns its own inner journal lock; this sibling
        # lock spans PREPARE -> SQLite COMMIT -> authority recovery/COMMIT so a second
        # resolver cannot mistake a live PREPARE for abandoned crash state.
        return WorkspaceEconomicLock(
            authority.journal_dir / "replay-cutoff-issuance"
        )

    def _frozen_replay_corpus_sha256(self, max_generation: int) -> str:
        if type(max_generation) is not int or max_generation < 0:
            raise ValueError("max_generation must be a non-negative int")
        qualified_columns = ",".join(
            f"m.{column}" for column in _HISTORY_COLUMNS
        )
        rows = self.connection.execute(
            f"""SELECT c.append_generation, {qualified_columns}
                FROM market_event_commit_order AS c
                JOIN market_events AS m ON m.dedupe_key = c.dedupe_key
                WHERE c.append_generation <= ?
                ORDER BY c.append_generation, m.dedupe_key""",
            (max_generation,),
        ).fetchall()
        encoded_rows: list[list[object]] = []
        for row in rows:
            if len(row) != len(_HISTORY_COLUMNS) + 1:
                raise ValueError("causal replay corpus authority is invalid")
            generation = row[0]
            history_row = tuple(row[1:])
            if type(generation) is not int or generation < 0:
                raise ValueError("causal replay corpus authority is invalid")

            # Do not let the independent machine authority bless malformed durable
            # event bytes. The digest format remains unchanged for compatibility,
            # but every row is first proven to be the exact canonical MarketEvent
            # represented by its redundant SQLite columns.
            _event_from_history_row(history_row)
            dedupe_key = history_row[0]
            payload_json = history_row[-1]
            if type(dedupe_key) is not str or type(payload_json) is not str:
                raise ValueError("causal replay corpus authority is invalid")
            encoded_rows.append([generation, dedupe_key, payload_json])
        return _canonical_sha256(
            {
                "schema": _REPLAY_CUTOFF_CORPUS_SCHEMA,
                "max_append_generation": max_generation,
                "rows": encoded_rows,
            }
        )

    def _recover_replay_cutoff_authority(
        self,
        authority: MonotonicWorkspaceAuthority,
        observed_state_sha256: str | None,
        *,
        cutoff_rows: tuple[tuple[str, str, int], ...],
    ) -> None:
        try:
            authority.recover(observed_state_sha256=observed_state_sha256)
            return
        except MonotonicAuthorityRecoveryRequiredError:
            history = authority.read_history()
            if not history or history[-1].phase is not AuthorityPhase.PREPARE:
                raise
            pending = history[-1]

            # Recovery may only COMMIT the exact one-row cutoff transition that the
            # product would have issued. Infer the previous durable row-set by
            # removing each current row in turn and matching the PREPARE ancestry;
            # then bind the newly added row to its exact frozen corpus digest.
            candidates: list[tuple[str, str, int]] = []
            for index, row in enumerate(cutoff_rows):
                prior_rows = cutoff_rows[:index] + cutoff_rows[index + 1 :]
                prior_state_sha256 = self._replay_cutoff_authority_state_sha256(
                    prior_rows
                )
                if prior_state_sha256 == pending.previous_committed_state_sha256:
                    candidates.append(row)
            if (
                len(candidates) != 1
                or pending.intended_state_sha256 != observed_state_sha256
            ):
                raise MonotonicAuthorityRollbackError(
                    "causal replay cutoff PREPARE is not one canonical row addition"
                )

            cutoff_id, canonical_as_of, max_generation = candidates[0]
            tx_prefix = f"{cutoff_id[:32]}-"
            tx_suffix = pending.tx_id[len(tx_prefix) :] if pending.tx_id.startswith(tx_prefix) else ""
            corpus_sha256 = self._frozen_replay_corpus_sha256(max_generation)
            expected_binding_sha256 = _replay_cutoff_binding_sha256(
                cutoff_id=cutoff_id,
                canonical_as_of=canonical_as_of,
                max_append_generation=max_generation,
                corpus_sha256=corpus_sha256,
            )
            if (
                len(tx_suffix) != 32
                or re.fullmatch(r"[0-9a-f]{32}", tx_suffix) is None
                or pending.semantic_binding_sha256 != expected_binding_sha256
            ):
                raise MonotonicAuthorityRollbackError(
                    "causal replay cutoff PREPARE semantic binding is invalid"
                )

            authority.recover(
                observed_state_sha256=observed_state_sha256,
                tx_id=pending.tx_id,
                semantic_binding_sha256=pending.semantic_binding_sha256,
            )

    @staticmethod
    def _require_independent_cutoff_issuance(
        authority: MonotonicWorkspaceAuthority,
        *,
        cutoff_id: str,
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

        issued = matches[0]
        tx_prefix = f"{cutoff_id[:32]}-"
        tx_suffix = (
            issued.tx_id[len(tx_prefix) :]
            if issued.tx_id.startswith(tx_prefix)
            else ""
        )
        if (
            len(tx_suffix) != 32
            or re.fullmatch(r"[0-9a-f]{32}", tx_suffix) is None
        ):
            raise MonotonicAuthorityRollbackError(
                "causal replay cutoff committed transaction identity is invalid"
            )

    def _rebuild_current_quotes(
        self,
        *,
        append_authority: MonotonicWorkspaceAuthority,
    ) -> None:
        """Repair current projection from history proven in the same write snapshot."""
        latest: dict[tuple[str, str], tuple[tuple[int, str], MarketEvent]] = {}
        stream_semantics: dict[tuple[str, str], tuple[str, str | None]] = {}
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            # Startup repair is itself a trust-boundary write. Re-prove both mutable
            # tables and the complete append chronology only after the SQLite write
            # snapshot is established, so a direct writer cannot alter history or
            # inject a projection trigger between authority proof and repair.
            _validate_canonical_table(self.connection, "market_events")
            _validate_canonical_table(self.connection, "current_quotes")
            self._validate_causal_replay_state()
            self._require_product_issued_positive_history(append_authority)

            rows = self.connection.execute(
                f"SELECT {_HISTORY_COLUMNS_SQL} FROM market_events"
            ).fetchall()
            for row in rows:
                event = _event_from_history_row(row)
                order_key = _projection_order_key(event)
                projection_key = (event.source_id, event.quote_key)
                expected_semantics = stream_semantics.get(projection_key)
                if expected_semantics is None:
                    stream_semantics[projection_key] = _stream_semantic_identity(event)
                else:
                    _assert_stream_semantic_identity(expected_semantics, event)
                previous = latest.get(projection_key)
                if previous is None or order_key > previous[0]:
                    latest[projection_key] = (order_key, event)

            # current_quotes is a derived cache, not an authority. Its old
            # payload bytes are deliberately not parsed: once canonical history and
            # append authority are proven in this write snapshot, every projection
            # row can be discarded and reconstructed from history alone.
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
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self._commit_stable_database_path()

    def _repair_current_projection_for_key(
        self,
        *,
        source_id: str,
        quote_key: str,
    ) -> MarketEvent:
        """Re-derive one repairable current row from canonical history only."""

        rows = self.connection.execute(
            f"""SELECT {_HISTORY_COLUMNS_SQL}
                FROM market_events
                WHERE source_id=? AND quote_key=?""",
            (source_id, quote_key),
        ).fetchall()
        if not rows:
            raise RuntimeError(
                "cannot project current quote without canonical market history"
            )

        expected_semantics: tuple[str, str | None] | None = None
        event: MarketEvent | None = None
        for row in rows:
            candidate = _event_from_history_row(row)
            if expected_semantics is None:
                expected_semantics = _stream_semantic_identity(candidate)
            else:
                _assert_stream_semantic_identity(expected_semantics, candidate)
            if (
                event is None
                or _projection_order_key(candidate) > _projection_order_key(event)
            ):
                event = candidate

        if event is None:
            raise RuntimeError(
                "cannot project current quote without canonical market history"
            )
        payload = _canonical_payload(event)
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
        return event

    def _insert_one(self, event: MarketEvent) -> bool:
        payload = _validate_incoming_event(event)
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
            self._repair_current_projection_for_key(
                source_id=event.source_id,
                quote_key=event.quote_key,
            )
            return False

        self.connection.execute(
            """INSERT INTO market_event_commit_order
               (dedupe_key, append_generation)
               VALUES (?, ?)""",
            (event.dedupe_key, self._next_append_generation()),
        )
        self._repair_current_projection_for_key(
            source_id=event.source_id,
            quote_key=event.quote_key,
        )
        return True

    def append(self, event: MarketEvent) -> bool:
        return bool(self.append_batch_accepted((event,)))

    def append_batch_accepted(self, events: Iterable[MarketEvent]) -> list[MarketEvent]:
        """Insert one normalized batch and independently issue its positive chronology."""

        # Consume caller-controlled iterables before taking any product authority or
        # SQLite writer lock. A generator may perform arbitrary I/O or re-enter this
        # store; executing it under the issuance lock/BEGIN IMMEDIATE would turn
        # caller code into part of the durable critical section and can deadlock or
        # stall live ingestion. Snapshot each yielded event immediately into its
        # canonical durable representation before asking the generator for the next
        # value, so later caller mutation of nested metadata cannot rewrite admitted
        # batch bytes during PREPARE -> SQLite COMMIT -> machine COMMIT.
        admitted: list[MarketEvent] = []
        try:
            iterator = iter(events)
        except TypeError as exc:
            raise TypeError("events must be an iterable of MarketEvent values") from exc
        for event in iterator:
            if not isinstance(event, MarketEvent):
                raise TypeError("events must contain only MarketEvent values")
            payload = _validate_incoming_event(event)
            admitted.append(MarketEvent.from_dict(_load_history_payload(payload)))
        batch = tuple(admitted)

        authority = self._market_append_authority()
        # Keep the global lock order identical to trusted readers: cross-process
        # append issuance first, then this instance's SQLite connection lock.
        # Reversing these two locks creates an AB-BA deadlock when one thread is
        # appending while another calls events()/current_by_source().
        with self._market_append_issuance_lock(authority):
            with self._connection_lock:
                self.connection.execute("BEGIN IMMEDIATE")
                prepared: tuple[str, str] | None = None
                accepted: list[MarketEvent] = []
                try:
                    # Re-prove the mutable live-storage schema at the write
                    # boundary.  A direct SQLite writer must not be able to add a
                    # trigger after startup and have product append bless trigger
                    # side effects into canonical history/current projection.
                    _validate_canonical_table(self.connection, "market_events")
                    _validate_canonical_table(self.connection, "current_quotes")
                    self._validate_causal_replay_state()
                    committed_head, committed_state_sha256 = (
                        self._recover_positive_append_authority(authority)
                    )
                    for event in batch:
                        if self._insert_one(event):
                            accepted.append(event)

                    if not accepted:
                        self._commit_stable_database_path()
                        return accepted

                    qualified_columns = ",".join(
                        f"m.{column}" for column in _HISTORY_COLUMNS
                    )
                    entries: list[tuple[int, str, str]] = []
                    for event in accepted:
                        row = self.connection.execute(
                            f"""SELECT c.append_generation, {qualified_columns}
                                FROM market_event_commit_order AS c
                                JOIN market_events AS m
                                  ON m.dedupe_key = c.dedupe_key
                                WHERE c.dedupe_key=?""",
                            (event.dedupe_key,),
                        ).fetchone()
                        if row is None or len(row) != len(_HISTORY_COLUMNS) + 1:
                            raise RuntimeError(
                                "accepted market event lacks append authority row"
                            )
                        generation = row[0]
                        history_row = tuple(row[1:])
                        _event_from_history_row(history_row)
                        dedupe_key = history_row[0]
                        payload_json = history_row[-1]
                        if (
                            type(generation) is not int
                            or type(dedupe_key) is not str
                            or type(payload_json) is not str
                        ):
                            raise ValueError(
                                "accepted market event append authority is invalid"
                            )
                        entries.append((generation, dedupe_key, payload_json))

                    entries.sort(key=lambda entry: entry[0])
                    expected_generation = committed_head + 1
                    intended_state_sha256 = committed_state_sha256
                    for generation, dedupe_key, payload_json in entries:
                        if generation != expected_generation:
                            raise MonotonicAuthorityRollbackError(
                                "positive market append chronology diverged during product append"
                            )
                        intended_state_sha256 = _append_state_step_sha256(
                            intended_state_sha256,
                            append_generation=generation,
                            dedupe_key=dedupe_key,
                            payload_json=payload_json,
                        )
                        expected_generation += 1
                    entry_tuple = tuple(entries)
                    binding_sha256 = _append_binding_sha256(
                        previous_state_sha256=committed_state_sha256,
                        intended_state_sha256=intended_state_sha256,
                        entries=entry_tuple,
                    )
                    start_generation = entry_tuple[0][0]
                    end_generation = entry_tuple[-1][0]
                    tx_id = (
                        f"append-{start_generation}-{end_generation}-{uuid.uuid4().hex}"
                    )
                    authority.prepare(
                        tx_id=tx_id,
                        observed_state_sha256=committed_state_sha256,
                        intended_state_sha256=intended_state_sha256,
                        semantic_binding_sha256=binding_sha256,
                    )
                    prepared = (tx_id, binding_sha256)
                    self._commit_stable_database_path()
                except BaseException:
                    # Once PREPARE exists, do not guess whether SQLite publication
                    # is durable. A commit can succeed before a post-commit pathname
                    # check fails, and rollback cannot undo that durable state. Leave
                    # the PREPARE as a crash prefix: the next append/trusted read
                    # recomputes canonical SQLite state and lets independent recovery
                    # either ABORT previous-state or COMMIT exact intended-state.
                    self.connection.rollback()
                    raise

                assert prepared is not None
                tx_id, binding_sha256 = prepared
                self._require_database_path_identity()
                authority.recover(
                    observed_state_sha256=intended_state_sha256,
                    tx_id=tx_id,
                    semantic_binding_sha256=binding_sha256,
                )
                return accepted

    def append_many(self, events: Iterable[MarketEvent]) -> int:
        return len(self.append_batch_accepted(events))

    def events(self, event_id: str | None = None) -> list[MarketEvent]:
        """Read only history proven against the independent append authority."""

        authority = self._market_append_authority()
        # A read must not recover or inspect the transient PREPARE of a live writer.
        # Take the same sibling issuance lock used by append, then prove the exact
        # canonical history before any row escapes to long-lived mirror/replay users.
        with self._market_append_issuance_lock(authority):
            with self._connection_lock:
                self.connection.execute("BEGIN")
                try:
                    _validate_canonical_table(self.connection, "market_events")
                    self._validate_causal_replay_state()
                    self._require_product_issued_positive_history(authority)
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
                    self._commit_stable_database_path()
                except BaseException:
                    self.connection.rollback()
                    raise
        return sorted(events, key=_event_order_key)

    def replay_events_at_frozen_cutoff(self, *, as_of: str) -> list[MarketEvent]:
        """Return the exact independently issued durable history cutoff for as_of.

        SQLite remains the canonical event/history store, but a cutoff row is accepted
        only when the existing machine-state MonotonicWorkspaceAuthority proves that
        product issuance. The independent state digest seals both the cutoff table and
        the exact corpus through the highest issued generation, so coherent same-DB
        DDL rewrites cannot be silently blessed by issuing a later cutoff.
        """

        canonical_as_of = _canonical_replay_cutoff(as_of)
        cutoff_id = _replay_cutoff_id(canonical_as_of)
        authority = self._replay_cutoff_authority()
        append_authority = self._market_append_authority()

        # Existing independently issued cutoffs never depend on later append-machine
        # progress, so keep them readable while another store is publishing a newer
        # append.  A first-time cutoff, however, must own the append sibling lock:
        # append commits SQLite before machine COMMIT, and BEGIN IMMEDIATE alone
        # therefore leaves a real SQLite-COMMIT -> machine-COMMIT window in which a
        # resolver could otherwise recover a still-live writer's PREPARE.
        # Lock order for first issuance is replay-cutoff sibling -> append sibling ->
        # this instance's SQLite connection. Append writers never acquire the replay
        # sibling, so there is no reverse dependency. Resolve the pre-existing hint
        # only after owning the replay sibling, so another resolver cannot publish the
        # same cutoff between admission and append-lock selection.
        with self._replay_cutoff_issuance_lock(authority):
            with self._connection_lock:
                preexisting_cutoff = (
                    self.connection.execute(
                        "SELECT 1 FROM market_replay_cutoffs WHERE cutoff_id=? LIMIT 1",
                        (cutoff_id,),
                    ).fetchone()
                    is not None
                )
            append_guard = (
                nullcontext()
                if preexisting_cutoff
                else self._market_append_issuance_lock(append_authority)
            )

            # Final escaping-row materialization happens after these coordination
            # locks. Canonical corpus validation may still decode rows here as part of
            # the authority proof; do not overstate this as a lock-free decode path.
            with append_guard, self._connection_lock:
                self._validate_causal_replay_state()
                cutoff_rows = self._validated_replay_cutoff_rows()
                observed_state_sha256 = (
                    self._replay_cutoff_authority_state_sha256(cutoff_rows)
                )
                self._recover_replay_cutoff_authority(
                    authority,
                    observed_state_sha256,
                    cutoff_rows=cutoff_rows,
                )

                current_row = next(
                    (
                        (stored_as_of, max_generation)
                        for stored_cutoff_id, stored_as_of, max_generation in cutoff_rows
                        if stored_cutoff_id == cutoff_id
                    ),
                    None,
                )

                if current_row is None and preexisting_cutoff:
                    raise MonotonicAuthorityRollbackError(
                        "pre-existing causal replay cutoff disappeared before proof"
                    )

                if current_row is None:
                    # Serialize only cutoff issuance against canonical appends. The
                    # potentially large replay scan/decode happens after the SQLite
                    # write transaction commits. The outer resolver lock stays held
                    # until the independent authority has recovered/committed the exact
                    # published cutoff state, preventing false abandonment of PREPARE.
                    self.connection.execute("BEGIN IMMEDIATE")
                    try:
                        self._validate_causal_replay_state()
                        self._require_product_issued_positive_history(
                            append_authority
                        )
                        cutoff_rows = self._validated_replay_cutoff_rows()
                        observed_state_sha256 = (
                            self._replay_cutoff_authority_state_sha256(cutoff_rows)
                        )
                        self._recover_replay_cutoff_authority(
                            authority,
                            observed_state_sha256,
                            cutoff_rows=cutoff_rows,
                        )
                        current_row = next(
                            (
                                (stored_as_of, max_generation)
                                for (
                                    stored_cutoff_id,
                                    stored_as_of,
                                    max_generation,
                                ) in cutoff_rows
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
                                        (
                                            cutoff_id,
                                            canonical_as_of,
                                            max_generation,
                                        ),
                                    ),
                                    key=lambda row: row[0],
                                )
                            )
                            intended_state_sha256 = _replay_cutoff_state_sha256(
                                intended_rows,
                                sealed_corpus_sha256=corpus_sha256,
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
                            self.connection.execute(
                                """INSERT INTO market_replay_cutoffs
                                   (cutoff_id, as_of, max_append_generation)
                                   VALUES (?, ?, ?)""",
                                (cutoff_id, canonical_as_of, max_generation),
                            )
                        self._commit_stable_database_path()
                    except BaseException:
                        # PREPARE is intentionally retained on every post-prepare
                        # failure. SQLite commit outcome and the final pathname
                        # identity can diverge across an exception boundary; durable
                        # recovery must decide from the actual cutoff row-set instead
                        # of an eager caller-supplied ABORT claim.
                        self.connection.rollback()
                        raise

                    # Use the actual committed SQLite state rather than trusting the
                    # intended digest passed to PREPARE. This also closes the crash
                    # window where SQLite committed but the machine authority did not.
                    cutoff_rows = self._validated_replay_cutoff_rows()
                    observed_state_sha256 = (
                        self._replay_cutoff_authority_state_sha256(cutoff_rows)
                    )
                    self._require_database_path_identity()
                    self._recover_replay_cutoff_authority(
                        authority,
                        observed_state_sha256,
                        cutoff_rows=cutoff_rows,
                    )

                # Re-read and independently prove the exact durable state after
                # issuance/recovery, then consume rows from that same SQLite read
                # snapshot. In WAL mode this deferred transaction does not take the
                # writer lock, but it prevents a second connection from changing a
                # previously verified frozen corpus between binding verification and
                # the SELECT whose rows escape to replay consumers.
                self.connection.execute("BEGIN")
                try:
                    self._validate_causal_replay_state()
                    cutoff_rows = self._validated_replay_cutoff_rows()
                    observed_state_sha256 = (
                        self._replay_cutoff_authority_state_sha256(cutoff_rows)
                    )
                    self._recover_replay_cutoff_authority(
                        authority,
                        observed_state_sha256,
                        cutoff_rows=cutoff_rows,
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
                    self._require_committed_append_authority_through(
                        append_authority,
                        max_generation,
                    )
                    corpus_sha256 = self._frozen_replay_corpus_sha256(max_generation)
                    expected_binding_sha256 = _replay_cutoff_binding_sha256(
                        cutoff_id=cutoff_id,
                        canonical_as_of=canonical_as_of,
                        max_append_generation=max_generation,
                        corpus_sha256=corpus_sha256,
                    )
                    self._require_independent_cutoff_issuance(
                        authority,
                        cutoff_id=cutoff_id,
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
                    self._commit_stable_database_path()
                except BaseException:
                    self.connection.rollback()
                    raise

        events = [_event_from_history_row(row) for row in rows]
        return sorted(events, key=_event_order_key)

    def current_by_source(self) -> dict[tuple[str, str], MarketEvent]:
        """Return only a projection proven to equal independently trusted history."""

        authority = self._market_append_authority()
        with self._market_append_issuance_lock(authority):
            with self._connection_lock:
                self.connection.execute("BEGIN")
                try:
                    _validate_canonical_table(self.connection, "market_events")
                    _validate_canonical_table(self.connection, "current_quotes")
                    self._validate_causal_replay_state()
                    self._require_product_issued_positive_history(authority)

                    history_rows = self.connection.execute(
                        f"SELECT {_HISTORY_COLUMNS_SQL} FROM market_events"
                    ).fetchall()
                    expected: dict[tuple[str, str], MarketEvent] = {}
                    for history_row in history_rows:
                        event = _event_from_history_row(history_row)
                        key = (event.source_id, event.quote_key)
                        previous = expected.get(key)
                        if (
                            previous is None
                            or _projection_order_key(event)
                            > _projection_order_key(previous)
                        ):
                            expected[key] = event

                    rows = self.connection.execute(
                        f"SELECT {_CURRENT_COLUMNS_SQL} FROM current_quotes"
                    ).fetchall()
                    current: dict[tuple[str, str], MarketEvent] = {}
                    for row in rows:
                        event = _event_from_current_row(row)
                        key = (event.source_id, event.quote_key)
                        if key in current:
                            raise ValueError(
                                "current quote projection contains duplicate provider key"
                            )
                        current[key] = event

                    if current.keys() != expected.keys() or any(
                        _canonical_payload(current[key])
                        != _canonical_payload(expected[key])
                        for key in expected
                    ):
                        raise ValueError(
                            "current quote projection diverges from canonical market history"
                        )
                    self._commit_stable_database_path()
                except BaseException:
                    self.connection.rollback()
                    raise
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