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


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _base_definition() -> dict:
    return {
        "eventId": "event-tt-1",
        "eventTypeId": "2593174",
        "marketType": "MATCH_ODDS",
        "status": "OPEN",
        "runners": [
            {"id": 101, "name": "Player A", "status": "ACTIVE"},
            {"id": 202, "name": "Player B", "status": "ACTIVE"},
        ],
    }


class BetfairMarketDefinitionIdentityTests(unittest.TestCase):
    def test_conflicting_redeclared_source_identity_fields_fail_closed(self) -> None:
        mutations = {
            "eventId": "event-tt-2",
            "eventTypeId": "999999",
            "marketType": "OTHER_MARKET",
        }
        for field, changed_value in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "market.jsonl"
                _write_jsonl(
                    source,
                    [
                        {
                            "op": "mcm",
                            "pt": _epoch_ms("2026-02-10T12:00:00Z"),
                            "mc": [
                                {
                                    "id": "1.234567890",
                                    "marketDefinition": _base_definition(),
                                    "rc": [{"id": 101, "ltp": 1.8}],
                                }
                            ],
                        },
                        {
                            "op": "mcm",
                            "pt": _epoch_ms("2026-02-10T12:01:00Z"),
                            "mc": [
                                {
                                    "id": "1.234567890",
                                    "marketDefinition": {field: changed_value},
                                }
                            ],
                        },
                    ],
                )

                with self.assertRaisesRegex(
                    ValueError,
                    rf"marketDefinition {field} changed for Betfair market 1\.234567890",
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
