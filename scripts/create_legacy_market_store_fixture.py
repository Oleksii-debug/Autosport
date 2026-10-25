"""Create the bounded legacy market-store fixture used by Windows lifecycle gates.

This helper is test infrastructure, not a product migration implementation.  It
creates canonical history through ``SQLiteMarketStore`` and then rewrites only
the old four-column ``current_quotes`` projection that the production opener
must migrate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import stat
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.storage import SQLiteMarketStore


_LEGACY_COLUMNS = ("quote_key", "observed_ts", "sequence", "payload_json")


def create_legacy_market_store_fixture(
    path: Path, *, source_sha: str,
) -> dict[str, object]:
    if re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ValueError("fixture requires a canonical source SHA")
    parent = path.parent
    parent_info = parent.lstat()
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise ValueError("fixture parent must be a real directory")
    if path.exists() or path.is_symlink():
        raise ValueError("fixture database must not already exist")

    event = MarketEvent(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        decimal_odds=Decimal("2.50"),
        observed_ts="2026-09-17T01:05:00Z",
        ingest_ts="2026-09-17T01:05:00Z",
        source_id="provider-a",
        sequence=7,
    )
    store = SQLiteMarketStore(path)
    try:
        store.append(event)
    finally:
        store.close()
    payload = json.dumps(
        event.to_dict(), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE current_quotes")
        connection.execute(
            """CREATE TABLE current_quotes (
                quote_key TEXT PRIMARY KEY,
                observed_ts TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            )"""
        )
        connection.execute(
            """INSERT INTO current_quotes
               (quote_key,observed_ts,sequence,payload_json) VALUES (?,?,?,?)""",
            (event.quote_key, event.observed_ts, event.sequence, payload),
        )
        columns = tuple(
            row[1] for row in connection.execute(
                "PRAGMA table_xinfo(current_quotes)"
            ).fetchall()
        )
        history_count = connection.execute(
            "SELECT COUNT(*) FROM market_events"
        ).fetchone()[0]
        current_count = connection.execute(
            "SELECT COUNT(*) FROM current_quotes"
        ).fetchone()[0]
    if columns != _LEGACY_COLUMNS or history_count != 1 or current_count != 1:
        raise ValueError("legacy fixture readback differs")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "status": "PASS",
        "evidence_class": "SOURCE_HARNESS_LEGACY_MARKET_STORE_FIXTURE",
        "source_sha": source_sha,
        "database_sha256": digest,
        "schema": "legacy_current_quotes_four_column_projection",
        "history_count": 1,
        "current_count": 1,
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args(argv)
    result = create_legacy_market_store_fixture(
        args.database, source_sha=args.source_sha
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
