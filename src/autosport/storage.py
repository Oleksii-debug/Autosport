from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .domain import MarketEvent


def _observed_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("observed_ts must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_ts must be timezone-aware ISO-8601")
    return parsed


def _event_order_key(event: MarketEvent) -> tuple[datetime, int, str]:
    return (_observed_instant(event.observed_ts), event.sequence, event.dedupe_key)


class SQLiteMarketStore:
    """Crash-safe append-only normalized market history plus current quote projection."""

    def __init__(self, path: str | Path = "autosport.db") -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self._init_schema()
        self._rebuild_current_quotes()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_events (
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
            );
            CREATE INDEX IF NOT EXISTS idx_market_events_order
                ON market_events(observed_ts, sequence);
            CREATE INDEX IF NOT EXISTS idx_market_events_event
                ON market_events(event_id, observed_ts, sequence);
            CREATE INDEX IF NOT EXISTS idx_market_events_quote
                ON market_events(quote_key, observed_ts, sequence);
            CREATE TABLE IF NOT EXISTS current_quotes (
                quote_key TEXT PRIMARY KEY,
                observed_ts TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def _rebuild_current_quotes(self) -> None:
        """Repair current projection from one write-locked physical-time history snapshot."""
        latest: dict[str, tuple[tuple[datetime, int, str], MarketEvent]] = {}
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute("SELECT payload_json FROM market_events").fetchall()
            for (payload_json,) in rows:
                event = MarketEvent.from_dict(json.loads(payload_json))
                order_key = _event_order_key(event)
                previous = latest.get(event.quote_key)
                if previous is None or order_key > previous[0]:
                    latest[event.quote_key] = (order_key, event)

            self.connection.execute("DELETE FROM current_quotes")
            for quote_key in sorted(latest):
                event = latest[quote_key][1]
                payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                self.connection.execute(
                    "INSERT INTO current_quotes(quote_key,observed_ts,sequence,payload_json) VALUES (?,?,?,?)",
                    (quote_key, event.observed_ts, event.sequence, payload),
                )
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _insert_one(self, event: MarketEvent) -> bool:
        incoming_key = _event_order_key(event)
        payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO market_events
               (dedupe_key,quote_key,event_id,market_id,selection_id,decimal_odds,observed_ts,source_id,sequence,payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
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
            return False
        previous = self.connection.execute(
            "SELECT payload_json FROM current_quotes WHERE quote_key=?",
            (event.quote_key,),
        ).fetchone()
        previous_event = MarketEvent.from_dict(json.loads(previous[0])) if previous is not None else None
        if previous_event is None or incoming_key > _event_order_key(previous_event):
            self.connection.execute(
                """INSERT INTO current_quotes(quote_key,observed_ts,sequence,payload_json)
                   VALUES (?,?,?,?)
                   ON CONFLICT(quote_key) DO UPDATE SET
                   observed_ts=excluded.observed_ts, sequence=excluded.sequence, payload_json=excluded.payload_json""",
                (event.quote_key, event.observed_ts, event.sequence, payload),
            )
        return True

    def append(self, event: MarketEvent) -> bool:
        with self.connection:
            return self._insert_one(event)

    def append_batch_accepted(self, events: Iterable[MarketEvent]) -> list[MarketEvent]:
        """Insert one normalized batch in one transaction and return only newly accepted events in input order."""
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
            rows = self.connection.execute("SELECT payload_json FROM market_events").fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload_json FROM market_events WHERE event_id=?",
                (event_id,),
            ).fetchall()
        events = [MarketEvent.from_dict(json.loads(row[0])) for row in rows]
        return sorted(events, key=_event_order_key)

    def current(self) -> dict[str, MarketEvent]:
        rows = self.connection.execute("SELECT quote_key,payload_json FROM current_quotes").fetchall()
        current: dict[str, MarketEvent] = {}
        for quote_key, payload_json in rows:
            event = MarketEvent.from_dict(json.loads(payload_json))
            _event_order_key(event)
            if event.quote_key != quote_key:
                raise ValueError("current quote projection identity mismatch")
            current[quote_key] = event
        return current

    def close(self) -> None:
        self.connection.close()
