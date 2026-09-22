from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore
from autosport.source_universe_commitment import (
    SourceUniverseCommitmentError,
    build_source_universe_commitment,
    verify_source_universe_commitment,
)


def _begin_pending(store: CollectorDeltaStore) -> int:
    return store._begin_collector_cycle(
        source_id="source-x",
        run_id="run-1",
        stream_epoch="epoch-1",
        attempted_at="2026-01-01T00:00:05+00:00",
    )


def _finish_zero_result_success(store: CollectorDeltaStore) -> None:
    cycle_seq = _begin_pending(store)
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


class SourceUniverseSamePathStoreReplacementTests(unittest.TestCase):
    def test_live_canonical_store_object_rejects_same_path_database_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical_path = root / "canonical.db"
            decoy_path = root / "decoy.db"

            canonical = CollectorDeltaStore(canonical_path)
            _begin_pending(canonical)

            decoy = CollectorDeltaStore(decoy_path)
            _finish_zero_result_success(decoy)
            favorable = build_source_universe_commitment(
                decoy,
                expected_store_path=decoy_path,
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=1,
            )
            self.assertTrue(favorable.provider_observation_complete)
            self.assertEqual(canonical.path, canonical_path)

            os.replace(decoy_path, canonical_path)

            self.assertEqual(canonical.path, canonical_path)
            with self.assertRaises(SourceUniverseCommitmentError):
                verify_source_universe_commitment(
                    canonical,
                    favorable,
                    expected_store_path=canonical_path,
                    expected_source_id="source-x",
                    expected_start_cycle_seq=1,
                    expected_end_cycle_seq=1,
                )


if __name__ == "__main__":
    unittest.main()
