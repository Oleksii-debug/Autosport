import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.dataset import ReplayDataset
from autosport.domain import MarketEvent


class MarketEventDeserializationIntegrityTests(unittest.TestCase):
    @staticmethod
    def _raw_event(**overrides):
        raw = {
            "event_id": "event-1",
            "market_id": "winner",
            "selection_id": "player-a",
            "decimal_odds": "1.80",
            "observed_ts": "2026-09-12T20:00:00+00:00",
            "source_id": "fixture:test",
            "sequence": 1,
            "market_type": "winner",
            "status": "open",
        }
        raw.update(overrides)
        return raw

    def test_from_dict_rejects_nonfinite_or_nonpositive_market_economics(self):
        for odds in ("NaN", "Infinity", "-Infinity", "1", "0"):
            with self.subTest(odds=odds), self.assertRaises(ValueError):
                MarketEvent.from_dict(self._raw_event(decimal_odds=odds))

    def test_from_dict_does_not_stringify_malformed_canonical_identity(self):
        for field in ("event_id", "market_id", "selection_id", "source_id"):
            for value in (None, 7, "", " padded "):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    MarketEvent.from_dict(self._raw_event(**{field: value}))

    def test_replay_dataset_fails_closed_before_returning_nonfinite_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            market_path = root / "market.jsonl"
            results_path = root / "results.json"
            market_path.write_text(
                json.dumps(self._raw_event(decimal_odds="Infinity")) + "\n",
                encoding="utf-8",
            )
            results_path.write_text("{}", encoding="utf-8")
            dataset = ReplayDataset(
                root=root,
                name="malformed",
                sport="table_tennis",
                market_path=market_path,
                results_path=results_path,
                market_sha256="0" * 64,
                results_sha256="0" * 64,
            )

            with self.assertRaisesRegex(ValueError, "decimal_odds must be finite"):
                dataset.load_market_events()

    def test_valid_serialized_market_event_remains_compatible(self):
        event = MarketEvent.from_dict(self._raw_event())

        self.assertEqual(event.event_id, "event-1")
        self.assertEqual(event.market_id, "winner")
        self.assertEqual(event.selection_id, "player-a")
        self.assertEqual(event.source_id, "fixture:test")
        self.assertEqual(event.decimal_odds, Decimal("1.80"))
        self.assertTrue(event.decimal_odds.is_finite())


if __name__ == "__main__":
    unittest.main()
