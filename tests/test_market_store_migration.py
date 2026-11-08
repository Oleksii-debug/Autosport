from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

from autosport.domain import MarketEvent
from autosport.market_store_migration import main, migrate_existing_market_store
from autosport.storage import SQLiteMarketStore


def _legacy_store(path):
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
    store.append(event)
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
    return event


def test_existing_legacy_store_migrates_and_second_run_is_idempotent(tmp_path):
    path = tmp_path / "стан з пробілами.db"
    event = _legacy_store(path)

    first = migrate_existing_market_store(path)
    assert first["status"] == "PASS"
    assert first["migration_applied"] is True
    assert first["history_count"] == first["current_count"] == 1
    assert first["real_money_execution"] is False
    assert first["human_tested"] is False
    assert first["nvda_verified"] is False

    second = migrate_existing_market_store(path)
    assert second["migration_applied"] is False
    assert second["state_sha256"] == first["state_sha256"]
    reopened = SQLiteMarketStore(path)
    try:
        assert reopened.current_by_source()[("provider-a", event.quote_key)] == event
    finally:
        reopened.close()


def test_cli_rejects_missing_store_without_creating_it(tmp_path, capsys):
    path = tmp_path / "missing.db"
    assert main(["--database", str(path)]) == 3
    assert capsys.readouterr().out.strip() == (
        "market_store_migration=FAIL_CLOSED reason=unsafe_or_invalid_store"
    )
    assert not path.exists()


def test_cli_rejects_malformed_store_without_disclosing_path(tmp_path, capsys):
    path = tmp_path / "private malformed store.db"
    path.write_bytes(b"not sqlite")

    assert main(["--database", str(path)]) == 3
    output = capsys.readouterr().out.strip()
    assert output == "market_store_migration=FAIL_CLOSED reason=unsafe_or_invalid_store"
    assert str(path) not in output


def test_symlink_store_is_rejected_without_mutating_target(tmp_path):
    target = tmp_path / "target.db"
    _legacy_store(target)
    before = target.read_bytes()
    alias = tmp_path / "alias.db"
    alias.symlink_to(target)

    try:
        migrate_existing_market_store(alias)
    except ValueError as exc:
        assert str(exc) == "market store must be an existing regular non-symlink file"
    else:
        raise AssertionError("symlink market store unexpectedly accepted")
    assert target.read_bytes() == before
