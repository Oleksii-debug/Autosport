from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from autosport.historical_corpus import _outcome_provenance
from autosport.outcome_revision import canonical_outcome_revision_id


_SOURCE = "official-results:revision-integration"
_REVEAL = datetime.fromisoformat("2026-01-01T12:00:00+00:00")
_IMPORT = datetime.fromisoformat("2026-01-02T00:05:00+00:00")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _initial(root: Path, outcomes: dict[str, str]) -> tuple[Path, str]:
    effective_at = "2026-01-01T10:00:00Z"
    revision_id = canonical_outcome_revision_id(
        source_identity=_SOURCE,
        kind="initial",
        effective_at=effective_at,
        quote_outcomes=outcomes,
    )
    path = root / "outcomes-v1.json"
    _write(
        path,
        {
            "source": _SOURCE,
            "quote_outcomes": outcomes,
            "revision": {
                "schema_version": 1,
                "kind": "initial",
                "effective_at": effective_at,
                "revision_id": revision_id,
                "supersedes_revision_id": None,
                "predecessor_record_file": None,
                "predecessor_record_sha256": None,
            },
        },
    )
    return path, revision_id


def _correction(
    root: Path,
    predecessor: Path,
    predecessor_id: str,
    outcomes: dict[str, str],
) -> tuple[Path, str]:
    effective_at = "2026-01-01T11:00:00Z"
    predecessor_sha = _sha(predecessor)
    revision_id = canonical_outcome_revision_id(
        source_identity=_SOURCE,
        kind="correction",
        effective_at=effective_at,
        quote_outcomes=outcomes,
        supersedes_revision_id=predecessor_id,
        predecessor_record_sha256=predecessor_sha,
    )
    path = root / "outcomes-v2.json"
    _write(
        path,
        {
            "source": _SOURCE,
            "quote_outcomes": outcomes,
            "revision": {
                "schema_version": 1,
                "kind": "correction",
                "effective_at": effective_at,
                "revision_id": revision_id,
                "supersedes_revision_id": predecessor_id,
                "predecessor_record_file": predecessor.name,
                "predecessor_record_sha256": predecessor_sha,
            },
        },
    )
    return path, revision_id


def _sealed_results(
    current: Path,
    *,
    outcomes: dict[str, str],
    revision_id: str,
    root_revision_id: str,
) -> dict:
    return {
        "schema_version": 1,
        "quote_outcomes": outcomes,
        "outcome_provenance": {
            "schema_version": 1,
            "kind": "historical_outcome_provenance",
            "source_identity": _SOURCE,
            "source_record_file": current.name,
            "source_record_sha256": _sha(current),
            "outcome_revision_id": revision_id,
            "outcome_lineage_root_revision_id": root_revision_id,
            "terms_reference": "https://example.test/results-terms",
            "retention_basis": "test-only authoritative revision fixture",
            "authority_reference": "test-authority:revision-integration",
            "available_at": "2026-01-01T11:30:00Z",
            "acquired_at": "2026-01-02T00:02:00Z",
            "verified_at": "2026-01-02T00:03:00Z",
            "licensing_or_retention_verified": True,
            "redistribution_policy": "internal_only",
            "redistribution_verified": False,
        },
    }


class HistoricalOutcomeRevisionIntegrationTests(unittest.TestCase):
    def test_predecessor_linked_correction_survives_canonical_provenance_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial, initial_id = _initial(root, {"q": "win"})
            current, current_id = _correction(root, initial, initial_id, {"q": "loss"})
            results = _sealed_results(
                current,
                outcomes={"q": "loss"},
                revision_id=current_id,
                root_revision_id=initial_id,
            )

            provenance = _outcome_provenance(
                results,
                source_root=root,
                reveal_dt=_REVEAL,
                imported_dt=_IMPORT,
            )

            self.assertTrue(provenance["outcome_revision_chain_verified"])
            self.assertEqual(provenance["outcome_revision_kind"], "correction")
            self.assertEqual(provenance["outcome_revision_id"], current_id)
            self.assertEqual(provenance["outcome_revision_supersedes_revision_id"], initial_id)
            self.assertEqual(provenance["outcome_lineage_root_revision_id"], initial_id)
            self.assertEqual(provenance["outcome_revision_chain_length"], 2)
            self.assertEqual(provenance["outcome_revision_predecessor_record_file"], initial.name)
            self.assertEqual(provenance["outcome_revision_predecessor_record_sha256"], _sha(initial))

    def test_same_authoritative_chain_revalidates_to_identical_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial, initial_id = _initial(root, {"q": "win"})
            current, current_id = _correction(root, initial, initial_id, {"q": "loss"})
            results = _sealed_results(
                current,
                outcomes={"q": "loss"},
                revision_id=current_id,
                root_revision_id=initial_id,
            )

            first = _outcome_provenance(
                results,
                source_root=root,
                reveal_dt=_REVEAL,
                imported_dt=_IMPORT,
            )
            second = _outcome_provenance(
                results,
                source_root=root,
                reveal_dt=_REVEAL,
                imported_dt=_IMPORT,
            )

            self.assertEqual(first, second)
            self.assertEqual(first["outcome_revision_id"], current_id)
            self.assertEqual(first["outcome_lineage_root_revision_id"], initial_id)

    def test_declared_current_revision_must_match_verified_source_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial, initial_id = _initial(root, {"q": "win"})
            current, _ = _correction(root, initial, initial_id, {"q": "loss"})
            results = _sealed_results(
                current,
                outcomes={"q": "loss"},
                revision_id="f" * 64,
                root_revision_id=initial_id,
            )

            with self.assertRaisesRegex(ValueError, "outcome_revision_id does not match"):
                _outcome_provenance(
                    results,
                    source_root=root,
                    reveal_dt=_REVEAL,
                    imported_dt=_IMPORT,
                )

    def test_declared_lineage_root_must_match_verified_predecessor_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial, initial_id = _initial(root, {"q": "win"})
            current, current_id = _correction(root, initial, initial_id, {"q": "loss"})
            results = _sealed_results(
                current,
                outcomes={"q": "loss"},
                revision_id=current_id,
                root_revision_id="e" * 64,
            )

            with self.assertRaisesRegex(ValueError, "outcome_lineage_root_revision_id does not match"):
                _outcome_provenance(
                    results,
                    source_root=root,
                    reveal_dt=_REVEAL,
                    imported_dt=_IMPORT,
                )


if __name__ == "__main__":
    unittest.main()
