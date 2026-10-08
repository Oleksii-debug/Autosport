from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from autosport.outcome_lineage import validate_outcome_source_lineage


class _HostileText(str):
    armed = False

    def strip(self, *args, **kwargs):
        if self.armed:
            raise AssertionError("hostile lineage text stripped before exact admission")
        return str.strip(self, *args, **kwargs)

    def encode(self, *args, **kwargs):
        if self.armed:
            raise AssertionError("hostile lineage text encoded before exact admission")
        return str.encode(self, *args, **kwargs)

    def __hash__(self) -> int:
        if self.armed:
            raise AssertionError("hostile lineage text hashed before exact admission")
        return str.__hash__(self)


class OutcomeLineagePathIntegrityTests(unittest.TestCase):
    def _root_record(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "source": "official-results:test-fixture",
            "record_id": "table-tennis-results:2026-01-01",
            "revision_id": "results-r1",
            "revision": 1,
            "revision_kind": "initial",
            "recorded_at": "2026-01-01T11:30:00+00:00",
            "quote_outcomes": {"tt-a|winner|alice": "win"},
        }

    def _validate(self, *, root: Path, source_file: str, record: dict[str, object]) -> None:
        validate_outcome_source_lineage(
            source_root=root,
            source_record_file=source_file,
            source_record_sha256="1" * 64,
            source_record=record,
            expected_source_identity="official-results:test-fixture",
        )

    def test_direct_identity_subclass_is_rejected_before_string_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hostile = _HostileText("official-results:test-fixture")
            hostile.armed = True

            with self.assertRaisesRegex(ValueError, "non-empty canonical string"):
                validate_outcome_source_lineage(
                    source_root=root,
                    source_record_file="head.json",
                    source_record_sha256="1" * 64,
                    source_record=self._root_record(),
                    expected_source_identity=hostile,
                )

    def test_frozen_record_rejects_hostile_mapping_key_before_hash_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = self._root_record()
            value = record.pop("source")
            hostile = _HostileText("source")
            record[hostile] = value
            hostile.armed = True

            with self.assertRaisesRegex(ValueError, "keys must be exact strings"):
                validate_outcome_source_lineage(
                    source_root=root,
                    source_record_file="head.json",
                    source_record_sha256="1" * 64,
                    source_record=record,
                    expected_source_identity="official-results:test-fixture",
                )

    def test_current_head_must_resolve_inside_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "results"
            root.mkdir()
            outside = parent / "outside-head.json"
            outside.write_text("{}", encoding="utf-8")
            link = root / "head.json"
            try:
                os.symlink(outside, link)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable on this platform: {exc}")

            with self.assertRaisesRegex(ValueError, "resolve to a direct sibling"):
                self._validate(
                    root=root,
                    source_file=link.name,
                    record=self._root_record(),
                )

    def test_root_revision_rejects_declared_null_correction_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for field in (
                "predecessor_record_file",
                "predecessor_record_sha256",
                "supersedes_revision_id",
                "correction_reason",
            ):
                record = self._root_record()
                record[field] = None
                with self.subTest(field=field):
                    with self.assertRaisesRegex(ValueError, "must not declare correction lineage fields"):
                        self._validate(
                            root=root,
                            source_file="head.json",
                            record=record,
                        )


if __name__ == "__main__":
    unittest.main()
