"""Invoke the canonical market-store schema migration from packaged tooling.

The migration itself remains owned by :class:`autosport.storage.SQLiteMarketStore`.
This module only supplies a bounded, path-redacted operator entry point which
requires an existing database and proves the migrated store can be reopened.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import stat
from pathlib import Path

from .storage import SQLiteMarketStore


_CURRENT_QUOTE_COLUMNS = (
    "source_id",
    "quote_key",
    "observed_ts",
    "sequence",
    "payload_json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _columns(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        row[1]
        for row in connection.execute("PRAGMA table_xinfo(current_quotes)").fetchall()
    )


def _state_sha256(store: SQLiteMarketStore) -> str:
    history = [event.to_dict() for event in store.events()]
    current = [
        {
            "source_id": source_id,
            "quote_key": quote_key,
            "event": event.to_dict(),
        }
        for (source_id, quote_key), event in sorted(store.current_by_source().items())
    ]
    payload = json.dumps(
        {"history": history, "current": current},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def migrate_existing_market_store(path: str | Path) -> dict[str, object]:
    """Open and re-open one existing store through the canonical migration path."""
    database = Path(path)
    info = database.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("market store must be an existing regular non-symlink file")
    before = _sha256(database)
    readonly_uri = database.resolve(strict=True).as_uri() + "?mode=ro"
    with sqlite3.connect(readonly_uri, uri=True) as source:
        initial_columns = _columns(source)

    first = SQLiteMarketStore(database)
    try:
        columns = _columns(first.connection)
        if columns != _CURRENT_QUOTE_COLUMNS:
            raise ValueError("market store did not reach the canonical projection schema")
        event_count = len(first.events())
        current_count = len(first.current_by_source())
        state_sha256 = _state_sha256(first)
    finally:
        first.close()

    after = _sha256(database)
    reopened = SQLiteMarketStore(database)
    try:
        if len(reopened.events()) != event_count:
            raise ValueError("market store history changed during migration readback")
        if len(reopened.current_by_source()) != current_count:
            raise ValueError("market store projection changed during migration readback")
        if _state_sha256(reopened) != state_sha256:
            raise ValueError("market store logical state changed during migration readback")
    finally:
        reopened.close()
    return {
        "status": "PASS",
        "state_kind": "canonical_market_store",
        "before_sha256": before,
        "after_sha256": after,
        "migration_applied": initial_columns != _CURRENT_QUOTE_COLUMNS,
        "state_sha256": state_sha256,
        "history_count": event_count,
        "current_count": current_count,
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate an existing Autosport market store using its canonical opener"
    )
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = migrate_existing_market_store(args.database)
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError):
        print("market_store_migration=FAIL_CLOSED reason=unsafe_or_invalid_store")
        return 3
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
