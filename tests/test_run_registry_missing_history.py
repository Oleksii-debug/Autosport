import tempfile
import unittest
from pathlib import Path

from autosport.run_registry import RunRegistry


class RunRegistryMissingHistoryTests(unittest.TestCase):
    def test_missing_registry_with_surviving_transaction_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path = root / "run_registry.json"
            transaction_dir = root / ".run-transactions" / "run-1"
            transaction_dir.mkdir(parents=True)
            (transaction_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "run registry is missing while durable run history exists",
            ):
                RunRegistry.initialize_pristine(registry_path)

            self.assertFalse(registry_path.exists())

    def test_missing_registry_with_surviving_summary_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path = root / "run_registry.json"
            (root / "run-run-1.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "run registry is missing while durable run history exists",
            ):
                RunRegistry.initialize_pristine(registry_path)

            self.assertFalse(registry_path.exists())

    def test_empty_transaction_root_does_not_block_fresh_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path = root / "run_registry.json"
            (root / ".run-transactions").mkdir()

            registry = RunRegistry.initialize_pristine(registry_path)

            self.assertTrue(registry_path.is_file())
            self.assertEqual(registry.strategy_ids(), ())


if __name__ == "__main__":
    unittest.main()
