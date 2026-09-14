import json
import tempfile
import unittest
from pathlib import Path

from autosport.run_registry import RepeatedExperimentError, RunRegistry


class RunRegistryGlobalRunIdIntegrityTests(unittest.TestCase):
    @staticmethod
    def _begin(
        registry: RunRegistry,
        *,
        market_sha256: str = "a" * 64,
        run_id: str = "shared-run",
        allow_repeat: bool = False,
        transaction_evidence: bool = True,
    ) -> str:
        kwargs = {}
        if transaction_evidence:
            kwargs = {
                "base_paper_book_sha256": "c" * 64,
                "base_decision_ledger_sha256": "d" * 64,
            }
        return registry.begin(
            market_sha256,
            "b" * 64,
            "strategy",
            run_id,
            allow_repeat=allow_repeat,
            **kwargs,
        )

    @staticmethod
    def _complete(registry: RunRegistry, key: str, root: Path, *, run_id: str = "shared-run") -> None:
        registry.complete(
            key,
            str(root / f"run-{run_id}.json"),
            paper_book_sha256="e" * 64,
            decision_ledger_sha256="f" * 64,
        )

    def test_base_run_id_cannot_be_reused_by_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "run_registry.json"
            registry = RunRegistry(path)
            base = self._begin(registry)
            self._complete(registry, base, root)
            before = path.read_bytes()

            with self.assertRaisesRegex(RepeatedExperimentError, "durable history"):
                self._begin(registry, allow_repeat=True)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(registry.get(base)["status"], "completed")

    def test_run_id_is_unique_across_dataset_identities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "run_registry.json"
            registry = RunRegistry(path)
            base = self._begin(registry)
            self._complete(registry, base, root)
            before = path.read_bytes()

            with self.assertRaisesRegex(RepeatedExperimentError, "durable history"):
                self._begin(registry, market_sha256="9" * 64)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(registry.get(base)["status"], "completed")

    def test_duplicate_persisted_run_id_fails_closed_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "run_registry.json"
            registry = RunRegistry(path)

            first = self._begin(
                registry,
                run_id="legacy-1",
                transaction_evidence=False,
            )
            registry.complete(first, "legacy-one.json")
            second = self._begin(
                registry,
                market_sha256="9" * 64,
                run_id="legacy-2",
                transaction_evidence=False,
            )
            registry.complete(second, "legacy-two.json")

            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["runs"][second]["run_id"] = "legacy-1"
            path.write_text(json.dumps(raw), encoding="utf-8")
            tampered_bytes = path.read_bytes()

            with self.assertRaisesRegex(ValueError, "duplicate run_id evidence"):
                RunRegistry(path)

            self.assertEqual(path.read_bytes(), tampered_bytes)


if __name__ == "__main__":
    unittest.main()
