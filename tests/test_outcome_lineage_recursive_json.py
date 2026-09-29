from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.outcome_lineage import validate_outcome_source_lineage


class OutcomeLineageRecursiveJsonTests(unittest.TestCase):
    def test_brackets_inside_valid_json_string_do_not_count_as_nesting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            predecessor = root / "outcomes-r1.json"
            predecessor_payload = {
                "schema_version": 2,
                "source": "official-results:test-fixture",
                "record_id": "table-tennis-results:2026-01-01",
                "revision_id": "results-r1",
                "revision": 1,
                "revision_kind": "initial",
                "recorded_at": "2026-01-01T11:00:00+00:00",
                "quote_outcomes": {"tt-a|winner|alice": "win"},
            }
            predecessor.write_text(
                json.dumps(predecessor_payload, sort_keys=True),
                encoding="utf-8",
            )
            predecessor_sha = hashlib.sha256(predecessor.read_bytes()).hexdigest()
            head = {
                "schema_version": 2,
                "source": "official-results:test-fixture",
                "record_id": "table-tennis-results:2026-01-01",
                "revision_id": "results-r2",
                "revision": 2,
                "revision_kind": "correction",
                "recorded_at": "2026-01-01T12:00:00+00:00",
                "quote_outcomes": {"tt-a|winner|alice": "void"},
                "predecessor_record_file": predecessor.name,
                "predecessor_record_sha256": predecessor_sha,
                "supersedes_revision_id": "results-r1",
                "correction_reason": "[" * 1500 + "escaped \\\" text" + "]" * 1500,
            }
            head_path = root / "outcomes-r2.json"
            head_path.write_text(json.dumps(head, sort_keys=True), encoding="utf-8")
            head_sha = hashlib.sha256(head_path.read_bytes()).hexdigest()

            lineage = validate_outcome_source_lineage(
                source_root=root,
                source_record_file=head_path.name,
                source_record_sha256=head_sha,
                source_record=head,
                expected_source_identity="official-results:test-fixture",
            )

            self.assertEqual(lineage.revision_id, "results-r2")

    def test_hash_correct_deep_predecessor_json_fails_at_value_error_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            predecessor = root / "outcomes-r1.json"
            predecessor_prefix = (
                '{"schema_version":2,'
                '"source":"official-results:test-fixture",'
                '"record_id":"table-tennis-results:2026-01-01",'
                '"revision_id":"results-r1",'
                '"revision":1,'
                '"revision_kind":"initial",'
                '"recorded_at":"2026-01-01T11:00:00+00:00",'
                '"quote_outcomes":{"tt-a|winner|alice":"win"},'
                '"unused":'
            )
            predecessor.write_text(
                predecessor_prefix + "[" * 2000 + "0" + "]" * 2000 + "}",
                encoding="utf-8",
            )
            predecessor_sha = hashlib.sha256(predecessor.read_bytes()).hexdigest()

            head = {
                "schema_version": 2,
                "source": "official-results:test-fixture",
                "record_id": "table-tennis-results:2026-01-01",
                "revision_id": "results-r2",
                "revision": 2,
                "revision_kind": "correction",
                "recorded_at": "2026-01-01T12:00:00+00:00",
                "quote_outcomes": {"tt-a|winner|alice": "void"},
                "predecessor_record_file": predecessor.name,
                "predecessor_record_sha256": predecessor_sha,
                "supersedes_revision_id": "results-r1",
                "correction_reason": "official result correction",
            }
            head_path = root / "outcomes-r2.json"
            head_path.write_text(json.dumps(head, sort_keys=True), encoding="utf-8")
            head_sha = hashlib.sha256(head_path.read_bytes()).hexdigest()

            with self.assertRaisesRegex(
                ValueError,
                "sealed outcome predecessor record is not readable valid JSON",
            ):
                validate_outcome_source_lineage(
                    source_root=root,
                    source_record_file=head_path.name,
                    source_record_sha256=head_sha,
                    source_record=head,
                    expected_source_identity="official-results:test-fixture",
                )


if __name__ == "__main__":
    unittest.main()
