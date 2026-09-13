import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.dataset import load_dataset
from autosport.paper import PaperBook
from autosport.session import AutosportSession


class _FailBeforePrecommitRuntime:
    def on_market_event(self, _event) -> None:
        raise RuntimeError("synthetic precommit strategy failure")

    def finalize_replay(self) -> None:
        raise AssertionError("replay should have failed before finalize")


class PrePrecommitAbortTests(unittest.TestCase):
    def test_strategy_failure_is_aborted_and_same_strategy_can_retry(self):
        dataset = load_dataset(Path("examples/tt_demo"))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = AutosportSession(workspace, "10000", strategy_id="observe-only-v1")
            try:
                with patch.object(
                    session,
                    "_runtime",
                    return_value=_FailBeforePrecommitRuntime(),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "synthetic precommit strategy failure",
                    ):
                        session.run_dataset(dataset)

                self.assertEqual(session.registry.in_progress(), ())
                book = PaperBook.load(workspace / "paper_book.json")
                self.assertEqual(book.balance, Decimal("10000"))
                self.assertEqual(book.tickets, {})
                self.assertEqual((workspace / "decisions.jsonl").read_text(encoding="utf-8"), "")

                registry = json.loads(
                    (workspace / "run_registry.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    [item["status"] for item in registry["runs"].values()],
                    ["aborted"],
                )
                manifests = sorted((workspace / ".run-transactions").glob("*/manifest.json"))
                self.assertEqual(len(manifests), 1)
                self.assertEqual(
                    json.loads(manifests[0].read_text(encoding="utf-8"))["phase"],
                    "aborted",
                )

                result = session.run_dataset(dataset)
                self.assertEqual(result.balance, Decimal("10000"))
                self.assertEqual(session.registry.in_progress(), ())

                registry = json.loads(
                    (workspace / "run_registry.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    sorted(item["status"] for item in registry["runs"].values()),
                    ["aborted", "completed"],
                )
                manifests = sorted((workspace / ".run-transactions").glob("*/manifest.json"))
                self.assertEqual(
                    sorted(
                        json.loads(path.read_text(encoding="utf-8"))["phase"]
                        for path in manifests
                    ),
                    ["aborted", "completed"],
                )
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
