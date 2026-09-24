from __future__ import annotations

import sqlite3
from decimal import Decimal

import autosport.storage as storage_module
from autosport.domain import MarketEvent
from autosport.storage import SQLiteMarketStore


class _NoFullHistoryFetchallCursor(sqlite3.Cursor):
    def execute(self, sql: str, parameters=()):
        self._autosport_sql = sql
        return super().execute(sql, parameters)

    def fetchall(self):
        normalized = " ".join(getattr(self, "_autosport_sql", "").upper().split())
        if (
            normalized.startswith("SELECT ")
            and " FROM MARKET_EVENTS" in normalized
            and " WHERE " not in normalized
        ):
            raise AssertionError(
                "SQLiteMarketStore reopen must stream full market history instead of fetchall()"
            )
        return super().fetchall()


class _NoFullHistoryFetchallConnection(sqlite3.Connection):
    def execute(self, sql: str, parameters=()):
        cursor = self.cursor(factory=_NoFullHistoryFetchallCursor)
        return cursor.execute(sql, parameters)


def _event(*, selection_id: str, sequence: int, odds: str) -> MarketEvent:
    timestamp = f"2026-09-22T08:00:{sequence:02d}+00:00"
    return MarketEvent(
        event_id="event-1",
        market_id="winner",
        selection_id=selection_id,
        decimal_odds=Decimal(odds),
        observed_ts=timestamp,
        ingest_ts=timestamp,
        source_id="provider-a",
        sequence=sequence,
    )


def test_reopen_streams_full_history_without_materializing_fetchall(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "market.db"
    old_home = _event(selection_id="home", sequence=1, odds="2.10")
    new_home = _event(selection_id="home", sequence=2, odds="2.20")
    away = _event(selection_id="away", sequence=3, odds="1.80")

    initial = SQLiteMarketStore(path)
    try:
        assert initial.append_many((old_home, new_home, away)) == 3
    finally:
        initial.close()

    original_connect = sqlite3.connect

    def guarded_connect(*args, **kwargs):
        kwargs["factory"] = _NoFullHistoryFetchallConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(storage_module.sqlite3, "connect", guarded_connect)

    reopened = SQLiteMarketStore(path)
    try:
        current = reopened.current_by_source()
        assert len(current) == 2
        assert current[("provider-a", old_home.quote_key)] == new_home
        assert current[("provider-a", away.quote_key)] == away
    finally:
        reopened.close()
