from __future__ import annotations

import json
import sqlite3
from pathlib import Path

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
            CREATE TABLE IF NOT EXISTS current_quotes (
                selection_id TEXT PRIMARY KEY,
                observed_ts TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                payload_json TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def append(self, event: MarketEvent) -> bool:
        payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        with self.connection:
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO market_events
                   (dedupe_key,event_id,market_id,selection_id,decimal_odds,observed_ts,source_id,sequence,payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    event.dedupe_key,
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
                "SELECT observed_ts, sequence FROM current_quotes WHERE selection_id=?",
                (event.selection_id,),
            ).fetchone()
            if previous is None or (event.observed_ts, event.sequence) >= (previous[0], previous[1]):
                self.connection.execute(
                    """INSERT INTO current_quotes(selection_id,observed_ts,sequence,payload_json)
                       VALUES (?,?,?,?)
                       ON CONFLICT(selection_id) DO UPDATE SET
                       observed_ts=excluded.observed_ts, sequence=excluded.sequence, payload_json=excluded.payload_json""",
                    (event.selection_id, event.observed_ts, event.sequence, payload),
                )
        return True

    def events(self, event_id: str | None = None) -> list[MarketEvent]:
        if event_id is None:
            rows = self.connection.execute(
                "SELECT payload_json FROM market_events ORDER BY observed_ts, sequence"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload_json FROM market_events WHERE event_id=? ORDER BY observed_ts, sequence",
                (event_id,),
            ).fetchall()
        return [MarketEvent.from_dict(json.loads(row[0])) for row in rows]

    def current(self) -> dict[str, MarketEvent]:
        rows = self.connection.execute("SELECT selection_id,payload_json FROM current_quotes").fetchall()
        return {row[0]: MarketEvent.from_dict(json.loads(row[1])) for row in rows}

    def close(self) -> None:
        self.connection.close()
