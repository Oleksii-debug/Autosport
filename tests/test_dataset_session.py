import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.session import AutosportSession


class DatasetSessionTests(unittest.TestCase):
    def test_dataset_hashes_and_end_to_end_session(self):
        dataset = load_dataset(Path("examples/tt_demo"))
        self.assertEqual(dataset.sport, "table_tennis")
        with tempfile.TemporaryDirectory() as tmp:
            session = AutosportSession(tmp, "10000")
            result = session.run_dataset(dataset)
            self.assertEqual(result.replay.event_count, 4)
            self.assertEqual(len(result.settled_ticket_ids), 1)
            self.assertEqual(result.balance, Decimal("10031.00"))
            self.assertEqual(result.evaluation.net_profit, Decimal("31.00"))
            session.close()
            restored = AutosportSession(tmp, "1")
            self.assertEqual(restored.book.balance, Decimal("10031.00"))
            restored.close()

    def test_tampered_market_dataset_fails_closed(self):
        source = Path("examples/tt_demo")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            for name in ("manifest.json", "market.jsonl", "results.json"):
                (target / name).write_bytes((source / name).read_bytes())
            with (target / "market.jsonl").open("a", encoding="utf-8") as handle:
                handle.write("{}\n")
            with self.assertRaisesRegex(ValueError, "market dataset hash mismatch"):
                load_dataset(target)


if __name__ == "__main__":
    unittest.main()
