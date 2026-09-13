from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from autosport.betfair_historical_import import import_betfair_historical


def _epoch_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def _definition(*, status: str, winner: int = 101) -> dict:
    runners = [
        {"id": 101, "name": "Player A", "status": "ACTIVE"},
        {"id": 202, "name": "Player B", "status": "ACTIVE"},
    ]
    if status == "CLOSED":
        runners = [
            {
                "id": 101,
                "name": "Player A",
                "status": "WINNER" if winner == 101 else "LOSER",
            },
            {
                "id": 202,
                "name": "Player B",
                "status": "WINNER" if winner == 202 else "LOSER",
            },
        ]
    return {
        "eventId": "event-tt-1",
        "eventTypeId": "2593174",
        "marketType": "MATCH_ODDS",
        "status": status,
        "eventName": "Player A v Player B",
        "runners": runners,
    }


def _stream(*, repeated_winner: int = 101) -> list[dict]:
    return [
        {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T12:00:00Z"),
            "mc": [
                {
                    "id": "1.234567890",
                    "marketDefinition": _definition(status="OPEN"),
                    "rc": [
                        {"id": 101, "ltp": 1.8},
                        {"id": 202, "ltp": 2.1},
                    ],
                }
            ],
        },
        {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T13:00:00Z"),
            "mc": [
                {
                    "id": "1.234567890",
                    "marketDefinition": _definition(status="CLOSED", winner=101),
                }
            ],
        },
        {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T13:05:00Z"),
            "mc": [
                {
                    "id": "1.234567890",
                    "marketDefinition": _definition(
                        status="CLOSED", winner=repeated_winner
                    ),
                }
            ],
        },
    ]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


class BetfairSettlementImmutabilityTests(unittest.TestCase):
    def test_identical_repeated_closed_keeps_first_reveal_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "market.jsonl"
            output = root / "dataset"
            _write_jsonl(source, _stream())

            import_betfair_historical(
                [source],
                output,
                acquired_at="2026-02-10T13:30:00Z",
                imported_at="2026-02-10T14:00:00Z",
                terms_reference="test-rights",
                retention_basis="test-retention",
            )

            results = json.loads((output / "results.json").read_text(encoding="utf-8"))
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(results["outcome_reveal_after"], "2026-02-10T13:00:00Z")
        self.assertEqual(
            manifest["governance"]["causality"]["outcome_reveal_after"],
            "2026-02-10T13:00:00Z",
        )
        self.assertEqual(
            results["quote_outcomes"],
            {
                "event-tt-1|1.234567890|101": "win",
                "event-tt-1|1.234567890|202": "loss",
            },
        )

    def test_conflicting_repeated_closed_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "market.jsonl"
            _write_jsonl(source, _stream(repeated_winner=202))

            with self.assertRaisesRegex(
                ValueError,
                r"final settlement changed for Betfair market 1\.234567890",
            ):
                import_betfair_historical(
                    [source],
                    root / "dataset",
                    acquired_at="2026-02-10T13:30:00Z",
                    imported_at="2026-02-10T14:00:00Z",
                    terms_reference="test-rights",
                    retention_basis="test-retention",
                )


if __name__ == "__main__":
    unittest.main()
