import tempfile
import unittest
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.integrity import sha256_file
from autosport.run_registry import MixedStrategyWorkspaceError
from autosport.session import AutosportSession


class StrategyWorkspaceIsolationTests(unittest.TestCase):
    def test_second_strategy_cannot_inherit_first_strategy_economics(self):
        dataset = load_dataset(Path("examples/tt_demo"))
        with tempfile.TemporaryDirectory() as tmp:
            baseline = AutosportSession(tmp, "10000", strategy_id="baseline-v1")
            baseline.run_dataset(dataset)
            baseline.close()

            workspace = Path(tmp)
            before = {
                "paper_book": sha256_file(workspace / "paper_book.json"),
                "ledger": sha256_file(workspace / "decisions.jsonl"),
                "registry": sha256_file(workspace / "run_registry.json"),
            }

            control = AutosportSession(tmp, "1", strategy_id="observe-only-v1")
            try:
                with self.assertRaisesRegex(
                    MixedStrategyWorkspaceError,
                    "separate workspace per strategy/plan",
                ):
                    control.run_dataset(dataset)
            finally:
                control.close()

            after = {
                "paper_book": sha256_file(workspace / "paper_book.json"),
                "ledger": sha256_file(workspace / "decisions.jsonl"),
                "registry": sha256_file(workspace / "run_registry.json"),
            }
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
