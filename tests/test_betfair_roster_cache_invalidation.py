from __future__ import annotations

import bz2
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from autosport.betfair_historical_import import import_betfair_historical
from autosport.dataset import load_dataset
from autosport.storage import SQLiteMarketStore


def _epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _runner(selection_id: int, name: str, status: str = "ACTIVE") -> dict:
    return {"id": selection_id, "name": name, "status": status}


def _definition(*, status: str, runners: list[dict]) -> dict:
    return {
        "eventId": "event-roster-cache",
        "eventTypeId": "2593174",
        "marketType": "MATCH_ODDS",
        "status": status,
        "eventName": "Player A v Player B",
        "inPlay": False,
        "betDelay": 0,
        "priceLadderDefinition": {"type": "CLASSIC"},
        "runners": runners,
    }


def _write(path: Path, lines: list[dict]) -> None:
    with bz2.open(path, "wt", encoding="utf-8") as handle:
        for item in lines:
            handle.write(json.dumps(item, sort_keys=True) + "\n")


class BetfairRosterCacheInvalidationTests(unittest.TestCase):
    def test_removed_runner_state_cannot_reappear_after_readd_without_fresh_image(self) -> None:
        lines = [
            {
                "op": "mcm",
                "pt": _epoch_ms("2026-02-10T12:00:00Z"),
                "mc": [
                    {
                        "id": "1.roster-cache",
                        "img": True,
                        "marketDefinition": _definition(
                            status="OPEN",
                            runners=[
                                _runner(101, "Player A"),
                                _runner(999, "Player B"),
                            ],
                        ),
                        "rc": [
                            {"id": 101, "atb": [[1.8, 20.0]]},
                            {"id": 999, "atb": [[1.9, 40.0]]},
                        ],
                    }
                ],
            },
            {
                "op": "mcm",
                "pt": _epoch_ms("2026-02-10T12:02:00Z"),
                "mc": [
                    {
                        "id": "1.roster-cache",
                        "marketDefinition": {
                            "runners": [_runner(101, "Player A")],
                        },
                    }
                ],
            },
            {
                "op": "mcm",
                "pt": _epoch_ms("2026-02-10T12:03:00Z"),
                "mc": [
                    {
                        "id": "1.roster-cache",
                        "marketDefinition": {"status": "SUSPENDED"},
                    }
                ],
            },
            {
                "op": "mcm",
                "pt": _epoch_ms("2026-02-10T12:04:00Z"),
                "mc": [
                    {
                        "id": "1.roster-cache",
                        "marketDefinition": {
                            "status": "OPEN",
                            "runners": [
                                _runner(101, "Player A"),
                                _runner(999, "Player B"),
                            ],
                        },
                        "rc": [
                            {"id": 999, "atb": [[1.95, 30.0]]},
                        ],
                    }
                ],
            },
            {
                "op": "mcm",
                "pt": _epoch_ms("2026-02-10T13:00:00Z"),
                "mc": [
                    {
                        "id": "1.roster-cache",
                        "marketDefinition": _definition(
                            status="CLOSED",
                            runners=[
                                _runner(101, "Player A", "LOSER"),
                                _runner(999, "Player B", "WINNER"),
                            ],
                        ),
                    }
                ],
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "market.bz2"
            output = root / "dataset"
            _write(source, lines)
            import_betfair_historical(
                [source],
                output,
                acquired_at="2026-02-10T13:30:00Z",
                imported_at="2026-02-10T14:00:00Z",
                terms_reference="test-rights",
                retention_basis="test-retention",
            )
            events = load_dataset(output).load_market_events()

        roster_removal_events = [
            event
            for event in events
            if event.selection_id == "999" and event.observed_ts == "2026-02-10T12:02:00Z"
        ]
        self.assertEqual(len(roster_removal_events), 1)
        removed = roster_removal_events[0]
        self.assertEqual(removed.metadata["price_semantics"], "betfair_runner_roster_removed")
        self.assertIs(removed.metadata["runner_roster_membership"], False)
        self.assertIs(removed.metadata["runner_removed_from_authoritative_roster"], True)
        self.assertIs(removed.metadata["execution_quote_verified"], False)
        self.assertIs(removed.metadata["paper_fill_eligible"], False)

        with tempfile.TemporaryDirectory() as projection_tmp:
            store = SQLiteMarketStore(Path(projection_tmp) / "market.db")
            try:
                store.append_many(
                    event
                    for event in events
                    if event.observed_ts <= "2026-02-10T12:02:00Z"
                )
                current = store.current()[removed.quote_key]
            finally:
                store.close()
        self.assertEqual(current.observed_ts, "2026-02-10T12:02:00Z")
        self.assertEqual(current.metadata["price_semantics"], "betfair_runner_roster_removed")

        removed_transition_events = [
            event
            for event in events
            if event.selection_id == "999" and event.observed_ts == "2026-02-10T12:03:00Z"
        ]
        self.assertEqual(removed_transition_events, [])

        readded = [
            event
            for event in events
            if event.selection_id == "999" and event.observed_ts == "2026-02-10T12:04:00Z"
        ]
        self.assertEqual(len(readded), 1)
        event = readded[0]
        self.assertEqual(str(event.decimal_odds), "1.95")
        self.assertEqual(event.metadata["price_semantics"], "betfair_available_to_back")
        self.assertIs(event.metadata["execution_quote_verified"], False)
        self.assertIs(event.metadata["paper_fill_eligible"], False)
        self.assertIn(
            "not initialized by a provider image",
            event.metadata["paper_fill_eligibility_reason"],
        )


if __name__ == "__main__":
    unittest.main()
