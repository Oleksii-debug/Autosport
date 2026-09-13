from __future__ import annotations

import bz2
import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from autosport.betfair_historical_import import import_betfair_historical
from autosport.dataset import load_dataset


def _epoch_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def _market_definition(*, status: str, event_type_id: str = "2593174") -> dict:
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
        "eventId": "event-tt-1",
        "eventTypeId": event_type_id,
        "marketType": "MATCH_ODDS",
        "status": status,
        "eventName": "Player A v Player B",
        "runners": runners,
    }


def _stream_lines(*, include_settlement: bool = True, event_type_id: str = "2593174") -> list[dict]:
    lines = [
        {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T12:00:00Z"),
            "mc": [
                {
                    "id": "1.234567890",
                    "marketDefinition": _market_definition(
                        status="OPEN", event_type_id=event_type_id
                    ),
                    "rc": [
                        {"id": 101, "ltp": 1.8},
                        {"id": 202, "ltp": 2.1},
                    ],
                }
            ],
        },
        {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T12:05:00Z"),
            "mc": [
                {
                    "id": "1.234567890",
                    "rc": [
                        {"id": 101, "ltp": 1.7},
                        {"id": 202, "ltp": 2.2},
                    ],
                }
            ],
        },
    ]
    if include_settlement:
        lines.append(
            {
                "op": "mcm",
                "pt": _epoch_ms("2026-02-10T13:00:00Z"),
                "mc": [
                    {
                        "id": "1.234567890",
                        "marketDefinition": _market_definition(
                            status="CLOSED", event_type_id=event_type_id
                        ),
                        # Settlement-message prices must never leak into the
                        # strategy-visible historical market file.
                        "rc": [
                            {"id": 101, "ltp": 1.01},
                            {"id": 202, "ltp": 50.0},
                        ],
                    }
                ],
            }
        )
    return lines


def _write_bz2(path: Path, lines: list[dict]) -> None:
    payload = "".join(json.dumps(item, sort_keys=True) + "\n" for item in lines)
    with bz2.open(path, "wt", encoding="utf-8") as handle:
        handle.write(payload)


class BetfairHistoricalImportTests(unittest.TestCase):
    def test_imports_table_tennis_match_odds_into_canonical_governed_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "market.bz2"
            output = root / "dataset"
            _write_bz2(source, _stream_lines())

            report = import_betfair_historical(
                [source],
                output,
                acquired_at="2026-02-10T13:30:00Z",
                imported_at="2026-02-10T14:00:00Z",
                terms_reference="local-test-rights-reference",
                retention_basis="local-test-retention-basis",
                redistribution_policy="prohibited",
            )

            dataset = load_dataset(output)
            events = dataset.load_market_events()
            outcomes = dataset.load_results_after_replay()
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            results = json.loads((output / "results.json").read_text(encoding="utf-8"))
            market_text = (output / "market.jsonl").read_text(encoding="utf-8")
            output_files = set(path.name for path in output.iterdir())

        self.assertEqual(dataset.schema_version, 2)
        self.assertEqual(dataset.sport, "table_tennis")
        self.assertEqual(dataset.import_identity, report.import_identity)
        self.assertEqual(report.market_event_count, 4)
        self.assertEqual(report.quote_count, 2)
        self.assertEqual(report.settled_market_count, 1)
        self.assertEqual(dataset.governance.source_ids, ("betfair_exchange_historical",))
        self.assertEqual(dataset.governance.market_types, ("winner",))
        self.assertEqual(dataset.governance.redistribution_policy, "prohibited")
        self.assertEqual(dataset.governance.coverage_start_ts, "2026-02-10T12:00:00Z")
        self.assertEqual(dataset.governance.coverage_end_ts, "2026-02-10T12:05:00Z")
        self.assertEqual(dataset.governance.outcome_reveal_after, "2026-02-10T13:00:00Z")
        self.assertEqual(results["outcome_reveal_after"], "2026-02-10T13:00:00Z")
        self.assertEqual(
            outcomes,
            {
                "event-tt-1|1.234567890|101": "win",
                "event-tt-1|1.234567890|202": "loss",
            },
        )
        self.assertEqual(
            {event.decimal_odds for event in events},
            {Decimal("1.8"), Decimal("2.1"), Decimal("1.7"), Decimal("2.2")},
        )
        self.assertTrue(all(event.source_ts == event.observed_ts for event in events))
        self.assertTrue(all(event.ingest_ts == "2026-02-10T14:00:00Z" for event in events))
        self.assertNotIn('"1.01"', market_text)
        self.assertNotIn('"50.0"', market_text)
        self.assertEqual(
            output_files,
            {"manifest.json", "market.jsonl", "results.json"},
        )
        self.assertEqual(manifest["import_identity"], report.import_identity)

    def test_missing_final_settlement_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "unsettled.bz2"
            _write_bz2(source, _stream_lines(include_settlement=False))

            with self.assertRaisesRegex(ValueError, "lack final settlement"):
                import_betfair_historical(
                    [source],
                    root / "dataset",
                    acquired_at="2026-02-10T13:30:00Z",
                    imported_at="2026-02-10T14:00:00Z",
                    terms_reference="test-rights",
                    retention_basis="test-retention",
                )

    def test_non_table_tennis_match_odds_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "wrong-sport.bz2"
            _write_bz2(source, _stream_lines(event_type_id="1"))

            with self.assertRaisesRegex(ValueError, "not Betfair Table Tennis"):
                import_betfair_historical(
                    [source],
                    root / "dataset",
                    acquired_at="2026-02-10T13:30:00Z",
                    imported_at="2026-02-10T14:00:00Z",
                    terms_reference="test-rights",
                    retention_basis="test-retention",
                )

    def test_duplicate_source_content_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.bz2"
            second = root / "second.bz2"
            _write_bz2(first, _stream_lines())
            second.write_bytes(first.read_bytes())

            with self.assertRaisesRegex(ValueError, "duplicate Betfair historical input content"):
                import_betfair_historical(
                    [first, second],
                    root / "dataset",
                    acquired_at="2026-02-10T13:30:00Z",
                    imported_at="2026-02-10T14:00:00Z",
                    terms_reference="test-rights",
                    retention_basis="test-retention",
                )


if __name__ == "__main__":
    unittest.main()
