import inspect
import tempfile
import unittest
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore
from autosport.source_universe_commitment import (
    SourceUniverseCommitmentError,
    build_source_universe_commitment,
)


class _DeceptiveReadDescriptor:
    """Looks canonical on class lookup but substitutes instance read authority."""

    def __init__(self, canonical):
        self._canonical = canonical
        self.instance_reads = 0

    def __get__(self, instance, owner):
        if instance is None:
            return self._canonical
        self.instance_reads += 1

        def poisoned(*_args, **_kwargs):
            raise AssertionError("deceptive descriptor reached instance read")

        return poisoned


class SourceUniverseDescriptorReadSeamTests(unittest.TestCase):
    def test_class_descriptor_cannot_hide_instance_read_rebind(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            cycle_seq = store._begin_collector_cycle(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                attempted_at="2026-01-01T00:00:05+00:00",
            )
            store._finish_collector_cycle(
                source_id="source-x",
                cycle_seq=cycle_seq,
                status="SUCCESS",
                completed_at="2026-01-01T00:00:06+00:00",
                catalog_changes=(),
                observed_delta_ids=(),
                committed_delta_ids=(),
                duplicate_delta_ids=(),
            )

            original = inspect.getattr_static(CollectorDeltaStore, "_connect")
            deceptive = _DeceptiveReadDescriptor(original)
            setattr(CollectorDeltaStore, "_connect", deceptive)
            try:
                with self.assertRaisesRegex(
                    SourceUniverseCommitmentError,
                    "class-rebound: _connect",
                ):
                    build_source_universe_commitment(
                        store,
                        expected_store_path=store.path,
                        source_id="source-x",
                        start_cycle_seq=1,
                        end_cycle_seq=1,
                    )
                self.assertEqual(deceptive.instance_reads, 0)
            finally:
                setattr(CollectorDeltaStore, "_connect", original)


if __name__ == "__main__":
    unittest.main()
