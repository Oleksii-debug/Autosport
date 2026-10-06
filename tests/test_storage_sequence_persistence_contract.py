import tempfile
import unittest
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.storage import SQLiteMarketStore


_SQLITE_INTEGER_MIN = -(2**63)
_SQLITE_INTEGER_MAX = 2**63 - 1


class StorageSequencePersistenceContractTests(unittest.TestCase):
    def _event(self, sequence: int, *, selection_id: str = "selection") -> MarketEvent:
        return MarketEvent.from_dict(
            {
                "event_id": "event",
                "market_id": "market",
                "selection_id": selection_id,
                "decimal_odds": "2.0",
                "observed_ts": "2026-01-01T00:00:00+00:00",
                "source_id": "source",
                "sequence": sequence,
            }
        )

    def test_signed_64_bit_boundaries_are_durable(self) -> None:
        lower = self._event(_SQLITE_INTEGER_MIN, selection_id="lower")
        upper = self._event(_SQLITE_INTEGER_MAX, selection_id="upper")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.db"
            store = SQLiteMarketStore(path)
            self.assertEqual(store.append_many([lower, upper]), 2)
            self.assertEqual(
                {event.sequence for event in store.events()},
                {_SQLITE_INTEGER_MIN, _SQLITE_INTEGER_MAX},
            )
            store.close()

            reopened = SQLiteMarketStore(path)
            self.assertEqual(
                {event.sequence for event in reopened.events()},
                {_SQLITE_INTEGER_MIN, _SQLITE_INTEGER_MAX},
            )
            reopened.close()

    def test_out_of_range_sequence_fails_before_sqlite_binding(self) -> None:
        for sequence in (_SQLITE_INTEGER_MIN - 1, _SQLITE_INTEGER_MAX + 1):
            with self.subTest(sequence=sequence), tempfile.TemporaryDirectory() as tmp:
                store = SQLiteMarketStore(Path(tmp) / "market.db")
                with self.assertRaisesRegex(
                    ValueError,
                    "must fit signed 64-bit SQLite INTEGER",
                ):
                    store.append(self._event(sequence))
                self.assertEqual(store.events(), [])
                self.assertEqual(store.current(), {})
                store.close()

    def test_invalid_late_batch_member_rolls_back_valid_prefix(self) -> None:
        valid = self._event(1, selection_id="valid")
        invalid = self._event(_SQLITE_INTEGER_MAX + 1, selection_id="invalid")

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            with self.assertRaisesRegex(
                ValueError,
                "must fit signed 64-bit SQLite INTEGER",
            ):
                store.append_batch_accepted([valid, invalid])
            self.assertEqual(store.events(), [])
            self.assertEqual(store.current(), {})
            store.close()


if __name__ == "__main__":
    unittest.main()
