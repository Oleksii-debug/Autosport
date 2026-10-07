import json
import tempfile
import unittest
from pathlib import Path

from autosport.outcome_trust import OutcomeLineageBinding, TrustedOutcomeRevision
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


class _HostileLineage(OutcomeLineageBinding):
    def __getattribute__(self, name: str):
        if name in {
            "source_identity",
            "record_id",
            "root_revision_id",
            "root_record_sha256",
            "revisions",
        }:
            raise AssertionError(f"hostile lineage {name} dispatched")
        return super().__getattribute__(name)


class _HostileRevision(TrustedOutcomeRevision):
    def __getattribute__(self, name: str):
        if name in {"revision", "revision_id", "record_sha256"}:
            raise AssertionError(f"hostile revision {name} dispatched")
        return super().__getattribute__(name)


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


    def test_begin_rejects_noncanonical_identity_alias_spellings_before_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_registry.json"
            registry = RunRegistry.initialize_pristine(path)
            baseline = path.read_bytes()

            cases = (
                ("strategy_id", " strategy"),
                ("strategy_id", "strategy\nvariant"),
                ("run_id", "run-1 "),
                ("run_id", "run-\x7f1"),
            )
            for field, malformed in cases:
                with self.subTest(field=field, malformed=repr(malformed)):
                    kwargs = {
                        "market_sha256": "a" * 64,
                        "results_sha256": "b" * 64,
                        "strategy_id": "strategy",
                        "run_id": "run-1",
                    }
                    kwargs[field] = malformed
                    with self.assertRaisesRegex(ValueError, "exact canonical"):
                        registry.begin(**kwargs)
                    self.assertEqual(path.read_bytes(), baseline)

    @staticmethod
    def _valid_lineage() -> OutcomeLineageBinding:
        return OutcomeLineageBinding(
            source_identity="provider",
            record_id="record",
            root_revision_id="rev-1",
            root_record_sha256="a" * 64,
            revisions=(
                TrustedOutcomeRevision(
                    revision=1,
                    revision_id="rev-1",
                    record_sha256="a" * 64,
                ),
            ),
        )

    def test_lineage_subclass_is_rejected_before_identity_attribute_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_registry.json"
            registry = RunRegistry.initialize_pristine(path)
            baseline = path.read_bytes()
            hostile = _HostileLineage(
                source_identity="provider",
                record_id="record",
                root_revision_id="rev-1",
                root_record_sha256="a" * 64,
                revisions=(
                    TrustedOutcomeRevision(
                        revision=1,
                        revision_id="rev-1",
                        record_sha256="a" * 64,
                    ),
                ),
            )

            with self.assertRaisesRegex(ValueError, "exact OutcomeLineageBinding"):
                registry.assert_outcome_lineage_compatible(hostile)
            with self.assertRaisesRegex(ValueError, "exact OutcomeLineageBinding"):
                registry.begin(
                    "a" * 64,
                    "b" * 64,
                    "strategy",
                    "run-1",
                    outcome_lineage=hostile,
                )
            self.assertEqual(path.read_bytes(), baseline)

    def test_lineage_rejects_hostile_identity_field_and_revision_subclass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_registry.json"
            registry = RunRegistry.initialize_pristine(path)
            baseline = path.read_bytes()

            hostile_identity = OutcomeLineageBinding(
                source_identity=_HostileIdentity("provider"),
                record_id="record",
                root_revision_id="rev-1",
                root_record_sha256="a" * 64,
                revisions=self._valid_lineage().revisions,
            )
            with self.assertRaisesRegex(ValueError, "exact non-empty string"):
                registry.assert_outcome_lineage_compatible(hostile_identity)

            hostile_revision = OutcomeLineageBinding(
                source_identity="provider",
                record_id="record",
                root_revision_id="rev-1",
                root_record_sha256="a" * 64,
                revisions=(
                    _HostileRevision(
                        revision=1,
                        revision_id="rev-1",
                        record_sha256="a" * 64,
                    ),
                ),
            )
            with self.assertRaisesRegex(ValueError, "exact TrustedOutcomeRevision"):
                registry.begin(
                    "a" * 64,
                    "b" * 64,
                    "strategy",
                    "run-1",
                    outcome_lineage=hostile_revision,
                )
            self.assertEqual(path.read_bytes(), baseline)

    def test_experiment_identity_rejects_subclasses_before_format_dispatch(self) -> None:
        cases = (
            (_HostileSha("a" * 64), "b" * 64, "strategy", "canonical SHA-256"),
            ("a" * 64, _HostileSha("b" * 64), "strategy", "canonical SHA-256"),
            ("a" * 64, "b" * 64, _HostileIdentity("strategy"), "exact non-empty string"),
        )
        for market_sha256, results_sha256, strategy_id, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    RunRegistry.experiment_identity(
                        market_sha256,
                        results_sha256,
                        strategy_id,
                    )

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
