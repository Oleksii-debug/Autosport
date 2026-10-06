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


def _rows(ltp: float) -> list[dict]:
    return [
        {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T12:00:00Z"),
            "mc": [
                {
                    "id": "1.234567890",
                    "marketDefinition": {
                        "eventId": "event-tt-1",
                        "eventTypeId": "2593174",
                        "marketType": "MATCH_ODDS",
                        "status": "OPEN",
                        "runners": [
                            {"id": 101, "name": "Player A", "status": "ACTIVE"},
                            {"id": 202, "name": "Player B", "status": "ACTIVE"},
                        ],
                    },
                    "rc": [
                        {"id": 101, "ltp": ltp},
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
                    "marketDefinition": {
                        "eventId": "event-tt-1",
                        "eventTypeId": "2593174",
                        "marketType": "MATCH_ODDS",
                        "status": "CLOSED",
                        "runners": [
                            {"id": 101, "name": "Player A", "status": "WINNER"},
                            {"id": 202, "name": "Player B", "status": "LOSER"},
                        ],
                    },
                }
            ],
        },
    ]


class BetfairNonFiniteLtpTests(unittest.TestCase):
    def test_non_finite_ltp_values_fail_closed(self) -> None:
        for bad_ltp in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad_ltp=bad_ltp), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "market.jsonl"
                output = root / "dataset"
                source.write_text(
                    "".join(json.dumps(row, sort_keys=True) + "\n" for row in _rows(bad_ltp)),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "ltp must be finite decimal odds > 1",
                ):
                    import_betfair_historical(
                        [source],
                        output,
                        acquired_at="2026-02-10T13:30:00Z",
                        imported_at="2026-02-10T14:00:00Z",
                        terms_reference="test-rights",
                        retention_basis="test-retention",
                    )

                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
