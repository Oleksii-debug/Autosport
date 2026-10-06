from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.outcome_lineage import validate_outcome_source_lineage


class OutcomeSourceLineageV2Tests(unittest.TestCase):
    source = "official-results:test-fixture"
    record_id = "table-tennis-results:2026-01-01"

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _record(
        self,
        *,
        revision: int,
        revision_id: str,
        recorded_at: str,
        outcome: str,
        predecessor: Path | None = None,
        predecessor_revision_id: str | None = None,
        source: str | None = None,
        record_id: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 2,
            "source": source or self.source,
            "record_id": record_id or self.record_id,
            "revision_id": revision_id,
            "revision": revision,
            "revision_kind": "initial" if revision == 1 else "correction",
            "recorded_at": recorded_at,
            "quote_outcomes": {"tt-a|winner|alice": outcome},
        }
        if predecessor is not None:
            payload.update(
                {
                    "predecessor_record_file": predecessor.name,
                    "predecessor_record_sha256": self._sha256(predecessor),
                    "supersedes_revision_id": predecessor_revision_id,
                    "correction_reason": "official result correction",
                }
            )
        return payload

    @staticmethod
    def _write(path: Path, payload: dict[str, object]) -> Path:
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return path

    def _validate(self, root: Path, head: Path, frozen: dict[str, object] | None = None):
        if frozen is None:
            frozen = json.loads(head.read_text(encoding="utf-8"))
        return validate_outcome_source_lineage(
            source_root=root,
            source_record_file=head.name,
            source_record_sha256=self._sha256(head),
            source_record=frozen,
            expected_source_identity=self.source,
        )

    def test_frozen_head_is_not_reopened_after_hash_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            head = self._write(
                root / "outcomes-r1.json",
                self._record(
                    revision=1,
                    revision_id="results-r1",
                    recorded_at="2026-01-01T11:00:00+00:00",
                    outcome="win",
                ),
            )
            frozen_bytes = head.read_bytes()
            frozen = json.loads(frozen_bytes.decode("utf-8"))
            frozen_sha = hashlib.sha256(frozen_bytes).hexdigest()
            head.write_text("this path changed after caller froze the head", encoding="utf-8")

            lineage = validate_outcome_source_lineage(
                source_root=root,
                source_record_file=head.name,
                source_record_sha256=frozen_sha,
                source_record=frozen,
                expected_source_identity=self.source,
            )

            self.assertEqual(lineage.revision_id, "results-r1")
            self.assertEqual(lineage.lineage_root_sha256, frozen_sha)
            self.assertEqual(lineage.lineage_depth, 1)

    def test_identical_root_validation_is_deterministic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            head = self._write(
                root / "outcomes-r1.json",
                self._record(
                    revision=1,
                    revision_id="results-r1",
                    recorded_at="2026-01-01T11:00:00+00:00",
                    outcome="win",
                ),
            )
            frozen = json.loads(head.read_text(encoding="utf-8"))
            first = self._validate(root, head, frozen)
            second = self._validate(root, head, frozen)
            self.assertEqual(first, second)
            self.assertEqual(first.record_id, self.record_id)
            self.assertEqual(first.lineage_root_revision_id, "results-r1")

    def test_hash_linked_correction_with_explicit_supersedes_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._write(
                root / "outcomes-r1.json",
                self._record(
                    revision=1,
                    revision_id="results-r1",
                    recorded_at="2026-01-01T11:00:00+00:00",
                    outcome="win",
                ),
            )
            second = self._write(
                root / "outcomes-r2.json",
                self._record(
                    revision=2,
                    revision_id="results-r2",
                    recorded_at="2026-01-01T12:00:00+00:00",
                    outcome="void",
                    predecessor=first,
                    predecessor_revision_id="results-r1",
                ),
            )

            lineage = self._validate(root, second)

            self.assertEqual(lineage.revision, 2)
            self.assertEqual(lineage.revision_id, "results-r2")
            self.assertEqual(lineage.supersedes_revision_id, "results-r1")
            self.assertEqual(lineage.predecessor_record_sha256, self._sha256(first))
            self.assertEqual(lineage.lineage_root_sha256, self._sha256(first))
            self.assertEqual(lineage.lineage_root_revision_id, "results-r1")
            self.assertEqual(lineage.lineage_depth, 2)
            self.assertEqual(lineage.quote_outcomes, {"tt-a|winner|alice": "void"})

    def test_three_revision_descriptor_is_complete_and_root_to_head_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._write(
                root / "outcomes-r1.json",
                self._record(
                    revision=1,
                    revision_id="results-r1",
                    recorded_at="2026-01-01T11:00:00+00:00",
                    outcome="win",
                ),
            )
            second = self._write(
                root / "outcomes-r2.json",
                self._record(
                    revision=2,
                    revision_id="results-r2",
                    recorded_at="2026-01-01T12:00:00+00:00",
                    outcome="void",
                    predecessor=first,
                    predecessor_revision_id="results-r1",
                ),
            )
            third = self._write(
                root / "outcomes-r3.json",
                self._record(
                    revision=3,
                    revision_id="results-r3",
                    recorded_at="2026-01-01T13:00:00+00:00",
                    outcome="loss",
                    predecessor=second,
                    predecessor_revision_id="results-r2",
                ),
            )

            lineage = self._validate(root, third)

            self.assertEqual(lineage.lineage_depth, 3)
            self.assertEqual(
                [revision.revision_id for revision in lineage.revisions],
                ["results-r1", "results-r2", "results-r3"],
            )
            self.assertEqual(
                [revision.record_sha256 for revision in lineage.revisions],
                [self._sha256(first), self._sha256(second), self._sha256(third)],
            )
            self.assertIsNone(lineage.revisions[0].predecessor_record_sha256)
            self.assertEqual(
                lineage.revisions[1].predecessor_record_sha256,
                self._sha256(first),
            )
            self.assertEqual(
                lineage.revisions[2].predecessor_record_sha256,
                self._sha256(second),
            )
            self.assertEqual(
                [revision.supersedes_revision_id for revision in lineage.revisions],
                [None, "results-r1", "results-r2"],
            )
            self.assertEqual(
                [revision.correction_reason for revision in lineage.revisions],
                [None, "official result correction", "official result correction"],
            )

    def test_correction_must_change_outcome_and_preserve_quote_key_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._write(
                root / "outcomes-r1.json",
                self._record(
                    revision=1,
                    revision_id="results-r1",
                    recorded_at="2026-01-01T11:00:00+00:00",
                    outcome="win",
                ),
            )
            second_payload = self._record(
                revision=2,
                revision_id="results-r2",
                recorded_at="2026-01-01T12:00:00+00:00",
                outcome="win",
                predecessor=first,
                predecessor_revision_id="results-r1",
            )
            second = self._write(root / "outcomes-r2.json", second_payload)
            with self.assertRaisesRegex(ValueError, "change at least one"):
                self._validate(root, second)

            second_payload["quote_outcomes"] = {"tt-a|winner|bob": "loss"}
            self._write(second, second_payload)
            with self.assertRaisesRegex(ValueError, "quote key set"):
                self._validate(root, second)

    def test_revision_above_one_requires_correction_lineage_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            head = self._write(
                root / "outcomes-r2.json",
                self._record(
                    revision=2,
                    revision_id="results-r2",
                    recorded_at="2026-01-01T12:00:00+00:00",
                    outcome="void",
                ),
            )
            with self.assertRaisesRegex(ValueError, "predecessor_record_file"):
                self._validate(root, head)

    def test_unlinked_or_mislinked_correction_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._write(
                root / "outcomes-r1.json",
                self._record(
                    revision=1,
                    revision_id="results-r1",
                    recorded_at="2026-01-01T11:00:00+00:00",
                    outcome="win",
                ),
            )
            head_payload = self._record(
                revision=2,
                revision_id="results-r2",
                recorded_at="2026-01-01T12:00:00+00:00",
                outcome="void",
                predecessor=first,
                predecessor_revision_id="not-the-predecessor",
            )
            head = self._write(root / "outcomes-r2.json", head_payload)
            with self.assertRaisesRegex(ValueError, "supersedes_revision_id"):
                self._validate(root, head)

            head_payload["supersedes_revision_id"] = "results-r1"
            head_payload["predecessor_record_sha256"] = "0" * 64
            self._write(head, head_payload)
            with self.assertRaisesRegex(ValueError, "predecessor SHA-256"):
                self._validate(root, head)

    def test_predecessor_identity_revision_and_timestamp_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_payload = self._record(
                revision=1,
                revision_id="results-r1",
                recorded_at="2026-01-01T12:00:00+00:00",
                outcome="win",
                record_id="wrong-record",
            )
            first = self._write(root / "outcomes-r1.json", first_payload)
            head = self._write(
                root / "outcomes-r2.json",
                self._record(
                    revision=2,
                    revision_id="results-r2",
                    recorded_at="2026-01-01T13:00:00+00:00",
                    outcome="void",
                    predecessor=first,
                    predecessor_revision_id="results-r1",
                ),
            )
            with self.assertRaisesRegex(ValueError, "record_id"):
                self._validate(root, head)

            first_payload["record_id"] = self.record_id
            first_payload["revision"] = 7
            first_payload["revision_kind"] = "correction"
            self._write(first, first_payload)
            head_payload = json.loads(head.read_text(encoding="utf-8"))
            head_payload["predecessor_record_sha256"] = self._sha256(first)
            self._write(head, head_payload)
            with self.assertRaisesRegex(ValueError, "revision"):
                self._validate(root, head)

            first_payload["revision"] = 1
            first_payload["revision_kind"] = "initial"
            first_payload["recorded_at"] = "2026-01-01T13:00:00+00:00"
            self._write(first, first_payload)
            head_payload["predecessor_record_sha256"] = self._sha256(first)
            self._write(head, head_payload)
            with self.assertRaisesRegex(ValueError, "strictly follow"):
                self._validate(root, head)

    def test_revision_one_cannot_smuggle_correction_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self._record(
                revision=1,
                revision_id="results-r1",
                recorded_at="2026-01-01T11:00:00+00:00",
                outcome="win",
            )
            payload["supersedes_revision_id"] = "older"
            head = self._write(root / "outcomes-r1.json", payload)
            with self.assertRaisesRegex(ValueError, "revision 1"):
                self._validate(root, head)

    def test_predecessor_strict_json_rejects_duplicate_keys_and_nan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            predecessor = root / "outcomes-r1.json"
            predecessor.write_bytes(
                b'{"schema_version":2,"source":"official-results:test-fixture",'
                b'"source":"official-results:ambiguous","record_id":"table-tennis-results:2026-01-01",'
                b'"revision_id":"results-r1","revision":1,"revision_kind":"initial",'
                b'"recorded_at":"2026-01-01T11:00:00+00:00",'
                b'"quote_outcomes":{"tt-a|winner|alice":"win"}}'
            )
            head = self._write(
                root / "outcomes-r2.json",
                self._record(
                    revision=2,
                    revision_id="results-r2",
                    recorded_at="2026-01-01T12:00:00+00:00",
                    outcome="void",
                    predecessor=predecessor,
                    predecessor_revision_id="results-r1",
                ),
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON object key: source"):
                self._validate(root, head)

            predecessor.write_bytes(
                b'{"schema_version":2,"source":"official-results:test-fixture",'
                b'"record_id":"table-tennis-results:2026-01-01","revision_id":"results-r1",'
                b'"revision":1,"revision_kind":"initial","recorded_at":"2026-01-01T11:00:00+00:00",'
                b'"quote_outcomes":{"tt-a|winner|alice":"win"},"confidence":NaN}'
            )
            head_payload = json.loads(head.read_text(encoding="utf-8"))
            head_payload["predecessor_record_sha256"] = self._sha256(predecessor)
            self._write(head, head_payload)
            with self.assertRaisesRegex(ValueError, "non-standard JSON constant: NaN"):
                self._validate(root, head)

    def test_lineage_validator_is_schema_v2_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            head = root / "legacy.json"
            head.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema_version"):
                validate_outcome_source_lineage(
                    source_root=root,
                    source_record_file=head.name,
                    source_record_sha256=self._sha256(head),
                    source_record={"source": self.source, "quote_outcomes": {}},
                    expected_source_identity=self.source,
                )


if __name__ == "__main__":
    unittest.main()
