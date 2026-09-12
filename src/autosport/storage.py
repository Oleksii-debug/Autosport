from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .domain import MarketEvent


class SQLiteMarketStore:
    """Crash-safe append-only normalized market history plus current quote projection."""

    def __init__(self, path: str | Path = "autosport.db") -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self._init_schema()

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

    def _insert_one(self, event: MarketEvent) -> bool:
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
            "SELECT observed_ts, sequence FROM current_quotes WHERE quote_key=?",
            (event.quote_key,),
        ).fetchone()
        if previous is None or (event.observed_ts, event.sequence) >= (previous[0], previous[1]):
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
            rows = self.connection.execute(
                "SELECT payload_json FROM market_events ORDER BY observed_ts, sequence, dedupe_key"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload_json FROM market_events WHERE event_id=? ORDER BY observed_ts, sequence, dedupe_key",
                (event_id,),
            ).fetchall()
        return [MarketEvent.from_dict(json.loads(row[0])) for row in rows]

    def current(self) -> dict[str, MarketEvent]:
        rows = self.connection.execute("SELECT quote_key,payload_json FROM current_quotes").fetchall()
        return {row[0]: MarketEvent.from_dict(json.loads(row[1])) for row in rows}

    def close(self) -> None:
        self.connection.close()
