import json
import tempfile
import unittest
from pathlib import Path

from autosport.run_registry import RunRegistry


class _HostileSha(str):
    def __len__(self) -> int:
        raise AssertionError("hostile SHA __len__ dispatched")

    def __iter__(self):
        raise AssertionError("hostile SHA __iter__ dispatched")


class _HostileIdentity(str):
    def __bool__(self) -> bool:
        raise AssertionError("hostile identity __bool__ dispatched")

    def __hash__(self) -> int:
        raise AssertionError("hostile identity __hash__ dispatched")

    def __format__(self, spec: str) -> str:
        raise AssertionError("hostile identity __format__ dispatched")


class RunRegistryIdentityExactTypeTests(unittest.TestCase):
    def test_begin_rejects_sha_subclasses_before_virtual_string_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_registry.json"
            registry = RunRegistry.initialize_pristine(path)
            baseline = path.read_bytes()

            for field in ("market_sha256", "results_sha256"):
                with self.subTest(field=field):
                    kwargs = {
                        "market_sha256": "a" * 64,
                        "results_sha256": "b" * 64,
                        "strategy_id": "strategy",
                        "run_id": "run-1",
                    }
                    kwargs[field] = _HostileSha("a" * 64)
                    with self.assertRaisesRegex(ValueError, "canonical SHA-256"):
                        registry.begin(**kwargs)
                    self.assertEqual(path.read_bytes(), baseline)
                    self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["runs"], {})

    def test_begin_rejects_strategy_and_run_id_subclasses_before_bool_or_format_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_registry.json"
            registry = RunRegistry.initialize_pristine(path)
            baseline = path.read_bytes()

            for field in ("strategy_id", "run_id"):
                with self.subTest(field=field):
                    kwargs = {
                        "market_sha256": "a" * 64,
                        "results_sha256": "b" * 64,
                        "strategy_id": "strategy",
                        "run_id": "run-1",
                    }
                    kwargs[field] = _HostileIdentity(str(kwargs[field]))
                    with self.assertRaisesRegex(ValueError, "exact non-empty string"):
                        registry.begin(**kwargs)
                    self.assertEqual(path.read_bytes(), baseline)
                    self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["runs"], {})


    def test_public_lookup_boundaries_reject_key_subclasses_before_hash_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "run_registry.json"
            registry = RunRegistry.initialize_pristine(path)
            key = registry.begin("a" * 64, "b" * 64, "strategy", "run-1")
            hostile = _HostileIdentity(key)
            baseline = path.read_bytes()

            operations = (
                ("get", lambda: registry.get(hostile)),
                ("complete", lambda: registry.complete(hostile)),
                (
                    "abort_uncommitted",
                    lambda: registry.abort_uncommitted(
                        hostile,
                        reason="abort",
                        paper_book_sha256="c" * 64,
                        decision_ledger_sha256="d" * 64,
                    ),
                ),
                (
                    "reconcile_completed_summary",
                    lambda: registry.reconcile_completed_summary(
                        hostile,
                        root / "run-run-1.json",
                        root / "paper_book.json",
                    ),
                ),
            )
            for operation, call in operations:
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(ValueError, "exact non-empty string"):
                        call()
                    self.assertEqual(path.read_bytes(), baseline)



if __name__ == "__main__":
    unittest.main()
