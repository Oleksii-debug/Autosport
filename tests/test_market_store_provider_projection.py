from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import pytest

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror
from autosport.storage import SQLiteMarketStore


def _event(
    *,
    source_id: str,
    sequence: int,
    observed_ts: str,
    decimal_odds: str,
) -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        decimal_odds=Decimal(decimal_odds),
        observed_ts=observed_ts,
        ingest_ts=observed_ts,
        source_id=source_id,
        sequence=sequence,
    )


def _replace_with_legacy_projection(
    path,
    *,
    event: MarketEvent,
) -> None:
    payload = json.dumps(
        event.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
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
               (quote_key,observed_ts,sequence,payload_json)
               VALUES (?,?,?,?)""",
            (event.quote_key, event.observed_ts, event.sequence, payload),
        )


def test_current_projection_is_provider_aware_sequence_authoritative_and_reopens(tmp_path):
    path = tmp_path / "market.db"
    store = SQLiteMarketStore(path)
    mirror = MarketMirror()

    source_a_old = _event(
        source_id="provider-a",
        sequence=1,
        observed_ts="2026-09-17T01:00:00Z",
        decimal_odds="2.10",
    )
    source_a_new = _event(
        source_id="provider-a",
        sequence=2,
        observed_ts="2026-09-17T00:59:00Z",
        decimal_odds="2.20",
    )
    source_b = _event(
        source_id="provider-b",
        sequence=1,
        observed_ts="2026-09-17T01:01:00Z",
        decimal_odds="2.30",
    )

    for event in (source_a_old, source_a_new, source_b):
        mirror.persist_and_apply(store, event)

    live_a = mirror.get("provider-a", "event-1", "winner", "home")
    assert live_a is not None
    assert live_a.sequence == 2

    durable = store.current_by_source()
    assert durable[("provider-a", source_a_old.quote_key)].sequence == 2
    assert durable[("provider-b", source_b.quote_key)].sequence == 1
    with pytest.raises(ValueError, match="ambiguous across providers"):
        store.current()

    store.close()
    reopened = SQLiteMarketStore(path)
    restored = MarketMirror.from_store(reopened)

    reopened_a = reopened.current_by_source()[("provider-a", source_a_old.quote_key)]
    restored_a = restored.get("provider-a", "event-1", "winner", "home")
    assert reopened_a.sequence == 2
    assert restored_a is not None
    assert restored_a.sequence == 2
    assert len(reopened.current_by_source()) == 2
    reopened.close()


def test_exact_legacy_current_projection_migrates_with_history_witness(tmp_path):
    path = tmp_path / "legacy.db"
    event = _event(
        source_id="provider-a",
        sequence=7,
        observed_ts="2026-09-17T01:05:00Z",
        decimal_odds="2.50",
    )
    store = SQLiteMarketStore(path)
    store.append(event)
    store.close()

    _replace_with_legacy_projection(path, event=event)

    reopened = SQLiteMarketStore(path)
    columns = reopened.connection.execute("PRAGMA table_xinfo(current_quotes)").fetchall()
    assert [row[1] for row in columns] == [
        "source_id",
        "quote_key",
        "observed_ts",
        "sequence",
        "payload_json",
    ]
    assert reopened.current_by_source()[("provider-a", event.quote_key)] == event
    reopened.close()


def test_legacy_projection_missing_history_witness_fails_closed_without_migration(tmp_path):
    path = tmp_path / "legacy-lost-history.db"
    history_event = _event(
        source_id="provider-a",
        sequence=1,
        observed_ts="2026-09-17T01:00:00Z",
        decimal_odds="2.10",
    )
    missing_event = _event(
        source_id="provider-b",
        sequence=1,
        observed_ts="2026-09-17T01:01:00Z",
        decimal_odds="2.20",
    )

    store = SQLiteMarketStore(path)
    store.append(history_event)
    store.close()
    _replace_with_legacy_projection(path, event=missing_event)

    with pytest.raises(ValueError, match="missing from authoritative history"):
        SQLiteMarketStore(path)

    with sqlite3.connect(path) as connection:
        columns = connection.execute("PRAGMA table_xinfo(current_quotes)").fetchall()
        assert [row[1] for row in columns] == [
            "quote_key",
            "observed_ts",
            "sequence",
            "payload_json",
        ]
        assert connection.execute("SELECT COUNT(*) FROM current_quotes").fetchone() == (1,)
