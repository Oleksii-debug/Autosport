from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.outcome_lineage import validate_outcome_source_lineage


class OutcomeLineageReadOnceCycleTests(unittest.TestCase):
    def test_self_predecessor_cycle_is_rejected_before_frozen_head_path_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            head = root / "outcomes-r2.json"
            frozen = {
                "schema_version": 2,
                "source": "official-results:test-fixture",
                "record_id": "table-tennis-results:2026-01-01",
                "revision_id": "results-r2",
                "revision": 2,
                "revision_kind": "correction",
                "recorded_at": "2026-01-01T12:00:00+00:00",
                "quote_outcomes": {"tt-a|winner|alice": "void"},
                "predecessor_record_file": head.name,
                "predecessor_record_sha256": "0" * 64,
                "supersedes_revision_id": "results-r1",
                "correction_reason": "malformed self-cycle",
            }

            with patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("frozen head path must not be re-opened"),
            ):
                with self.assertRaisesRegex(ValueError, "path cycle"):
                    validate_outcome_source_lineage(
                        source_root=root,
                        source_record_file=head.name,
                        source_record_sha256="1" * 64,
                        source_record=frozen,
                        expected_source_identity="official-results:test-fixture",
                    )


if __name__ == "__main__":
    unittest.main()
