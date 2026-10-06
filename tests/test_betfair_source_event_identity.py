from __future__ import annotations

import bz2
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from autosport.betfair_historical_import import import_betfair_historical


def _epoch_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    return int(parsed.timestamp() * 1000)


class BetfairSourceEventIdentityTests(unittest.TestCase):
    def test_supported_quote_without_source_event_id_fails_closed(self):
        message = {
            "op": "mcm",
            "pt": _epoch_ms("2026-02-10T12:00:00Z"),
            "mc": [
                {
                    "id": "1.234567890",
                    "marketDefinition": {
                        # Deliberately no eventId: market identity must never be
                        # substituted for source event identity.
                        "eventTypeId": "2593174",
                        "marketType": "MATCH_ODDS",
                        "status": "OPEN",
                        "eventName": "Player A v Player B",
                        "runners": [
                            {"id": 101, "name": "Player A", "status": "ACTIVE"},
                            {"id": 202, "name": "Player B", "status": "ACTIVE"},
                        ],
                    },
                    "rc": [{"id": 101, "ltp": 1.8}],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "missing-event-id.bz2"
            with bz2.open(source, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(message, sort_keys=True) + "\n")

            with self.assertRaisesRegex(ValueError, "requires source eventId"):
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
