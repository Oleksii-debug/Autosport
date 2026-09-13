from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from autosport.betfair_historical_import import import_betfair_historical
from autosport.dataset import load_dataset
from autosport.replay import ReplayEngine


def _epoch_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _definition(*, event_id: str, status: str) -> dict:
    runners = [
        {"id": 101, "name": "Player A", "status": "ACTIVE"},
        {"id": 202, "name": "Player B", "status": "ACTIVE"},
    ]
    if status == "CLOSED":
        runners = [
            {"id": 101, "name": "Player A", "status": "WINNER"},
            {"id": 202, "name": "Player B", "status": "LOSER"},
        ]
    return {
        "eventId": event_id,
        "eventTypeId": "2593174",
        "marketType": "MATCH_ODDS",
        "status": status,
        "eventName": f"{event_id} match",
        "runners": runners,
    }


def _stream(*, event_id: str, market_id: str, quote_at: str = "2026-02-10T12:00:00Z") -> list[dict]:
    return [
        {
            "op": "mcm",
            "pt": _epoch_ms(quote_at),
            "mc": [
                {
                    "id": market_id,
                    "marketDefinition": _definition(event_id=event_id, status="OPEN"),
                    "rc": [{"id": 101, "ltp": 1.8}, {"id": 202, "ltp": 2.1}],
                }
            ],
        },
        {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T13:00:00Z"),
            "mc": [
                {
                    "id": market_id,
                    "marketDefinition": _definition(event_id=event_id, status="CLOSED"),
                }
            ],
        },
    ]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


class BetfairPriceAndCausalityTruthTests(unittest.TestCase):
    def test_ltp_non_execution_truth_survives_manifest_load_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "market.jsonl"
            output = root / "dataset"
            _write_jsonl(source, _stream(event_id="event-1", market_id="1.111"))

            report = import_betfair_historical(
                [source],
                output,
                acquired_at="2026-02-10T13:30:00Z",
                imported_at="2026-02-10T14:00:00Z",
                terms_reference="test-rights",
                retention_basis="test-retention",
            )
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            dataset = load_dataset(output)
            events = dataset.load_market_events()
            replayed = []
            ReplayEngine(events).run(replayed.append)

        self.assertEqual(report.import_identity, manifest["import_identity"])
        self.assertEqual(
            manifest["governance"]["price_evidence"],
            {
                "price_semantics": "last_traded_price",
                "execution_quote_verified": False,
                "market_availability_history_complete_verified": False,
            },
        )
        self.assertTrue(events)
        self.assertEqual(len(replayed), len(events))
        for event in replayed:
            self.assertEqual(event.metadata["price_semantics"], "last_traded_price")
            self.assertIs(event.metadata["execution_quote_verified"], False)
            self.assertIs(event.metadata["market_availability_history_complete_verified"], False)

    def test_equal_replay_timestamp_across_different_source_files_fails_closed_regardless_of_input_order(self) -> None:
        for reverse in (False, True):
            with self.subTest(reverse=reverse), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                first = root / "first.jsonl"
                second = root / "second.jsonl"
                _write_jsonl(first, _stream(event_id="event-1", market_id="1.111"))
                _write_jsonl(second, _stream(event_id="event-2", market_id="1.222"))
                inputs = [second, first] if reverse else [first, second]
                output = root / "dataset"

                with self.assertRaisesRegex(
                    ValueError,
                    "equal replay-visible publish time across source files",
                ):
                    import_betfair_historical(
                        inputs,
                        output,
                        acquired_at="2026-02-10T13:30:00Z",
                        imported_at="2026-02-10T14:00:00Z",
                        terms_reference="test-rights",
                        retention_basis="test-retention",
                    )

                self.assertFalse(output.exists())

    def test_distinct_replay_timestamps_across_source_files_remain_importable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            output = root / "dataset"
            _write_jsonl(first, _stream(event_id="event-1", market_id="1.111", quote_at="2026-02-10T12:00:00Z"))
            _write_jsonl(second, _stream(event_id="event-2", market_id="1.222", quote_at="2026-02-10T12:01:00Z"))

            import_betfair_historical(
                [first, second],
                output,
                acquired_at="2026-02-10T13:30:00Z",
                imported_at="2026-02-10T14:00:00Z",
                terms_reference="test-rights",
                retention_basis="test-retention",
            )
            events = load_dataset(output).load_market_events()

        self.assertEqual(len(events), 4)
        self.assertEqual(
            [event.observed_ts for event in events],
            sorted(event.observed_ts for event in events),
        )


if __name__ == "__main__":
    unittest.main()
