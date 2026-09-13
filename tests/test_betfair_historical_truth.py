from __future__ import annotations

import bz2
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from autosport.betfair_historical_import import import_betfair_historical
from autosport.dataset import load_dataset


def _epoch_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def _market_definition(*, event_id: str, status: str) -> dict:
    runner_status = "WINNER" if status == "CLOSED" else "ACTIVE"
    return {
        "eventId": event_id,
        "eventTypeId": "2593174",
        "marketType": "MATCH_ODDS",
        "status": status,
        "eventName": f"{event_id} table tennis match",
        "runners": [
            {"id": 101, "name": "Player A", "status": runner_status},
        ],
    }


def _stream(*, market_id: str, event_id: str, open_ts: str = "2026-02-10T12:00:00Z") -> list[dict]:
    return [
        {
            "op": "mcm",
            "pt": _epoch_ms(open_ts),
            "mc": [
                {
                    "id": market_id,
                    "marketDefinition": _market_definition(event_id=event_id, status="OPEN"),
                    "rc": [{"id": 101, "ltp": 1.8}],
                }
            ],
        },
        {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T13:00:00Z"),
            "mc": [
                {
                    "id": market_id,
                    "marketDefinition": _market_definition(event_id=event_id, status="CLOSED"),
                }
            ],
        },
    ]


def _write_bz2(path: Path, lines: list[dict]) -> None:
    with bz2.open(path, "wt", encoding="utf-8") as handle:
        for item in lines:
            handle.write(json.dumps(item, sort_keys=True) + "\n")


class BetfairHistoricalTruthTests(unittest.TestCase):
    def test_ltp_semantics_survive_canonical_load_and_identity_bound_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "market.bz2"
            output = root / "dataset"
            _write_bz2(source, _stream(market_id="1.100", event_id="event-1"))

            report = import_betfair_historical(
                [source],
                output,
                acquired_at="2026-02-10T13:30:00Z",
                imported_at="2026-02-10T14:00:00Z",
                terms_reference="test-rights",
                retention_basis="test-retention",
            )

            dataset = load_dataset(output)
            event = dataset.load_market_events()[0]
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(dataset.import_identity, report.import_identity)
        self.assertEqual(event.metadata["price_semantics"], "betfair_last_traded_price")
        self.assertEqual(event.metadata["provider_price_field"], "rc[].ltp")
        self.assertIs(event.metadata["execution_quote_verified"], False)
        self.assertEqual(
            manifest["governance"]["price_semantics"],
            {
                "decimal_odds": "betfair_last_traded_price",
                "provider_field": "rc[].ltp",
                "execution_quote_verified": False,
            },
        )
        self.assertEqual(
            manifest["governance"]["availability_semantics"],
            {
                "strategy_visible_market_status": "OPEN_ONLY",
                "suspended_or_non_open_intervals_preserved": False,
                "complete_availability_history_verified": False,
            },
        )

    def test_equal_publish_time_across_different_source_files_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.bz2"
            second = root / "second.bz2"
            output = root / "dataset"
            _write_bz2(first, _stream(market_id="1.100", event_id="event-1"))
            _write_bz2(second, _stream(market_id="1.200", event_id="event-2"))

            with self.assertRaisesRegex(
                ValueError,
                "replay-visible events share publish time across input files.*cross-file source order is ambiguous",
            ):
                import_betfair_historical(
                    [first, second],
                    output,
                    acquired_at="2026-02-10T13:30:00Z",
                    imported_at="2026-02-10T14:00:00Z",
                    terms_reference="test-rights",
                    retention_basis="test-retention",
                )

            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
