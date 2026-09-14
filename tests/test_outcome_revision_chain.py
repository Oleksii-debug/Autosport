from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from autosport.outcome_revision import (
    canonical_outcome_revision_id,
    verify_outcome_revision_chain,
)


_SOURCE = "official-results:test-fixture"
_AVAILABLE = datetime.fromisoformat("2026-01-01T12:00:00+00:00")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _initial_record(outcomes: dict[str, str], *, effective_at: str = "2026-01-01T11:00:00Z") -> dict:
    revision_id = canonical_outcome_revision_id(
        source_identity=_SOURCE,
        kind="initial",
        effective_at=effective_at,
        quote_outcomes=outcomes,
    )
    return {
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
    }


def _correction_record(
    root: Path,
    outcomes: dict[str, str],
    predecessor: Path,
    *,
    effective_at: str = "2026-01-01T11:30:00Z",
    supersedes: str | None = None,
) -> dict:
    predecessor_raw = json.loads(predecessor.read_text(encoding="utf-8"))
    predecessor_id = predecessor_raw["revision"]["revision_id"]
    predecessor_sha = _sha(predecessor)
    supersedes = predecessor_id if supersedes is None else supersedes
    revision_id = canonical_outcome_revision_id(
        source_identity=_SOURCE,
        kind="correction",
        effective_at=effective_at,
        quote_outcomes=outcomes,
        supersedes_revision_id=supersedes,
        predecessor_record_sha256=predecessor_sha,
    )
    return {
        "source": _SOURCE,
        "quote_outcomes": outcomes,
        "revision": {
            "schema_version": 1,
            "kind": "correction",
            "effective_at": effective_at,
            "revision_id": revision_id,
            "supersedes_revision_id": supersedes,
            "predecessor_record_file": predecessor.name,
            "predecessor_record_sha256": predecessor_sha,
        },
    }


def _verify(root: Path, current: Path):
    record = json.loads(current.read_text(encoding="utf-8"))
    return verify_outcome_revision_chain(
        source_root=root,
        source_record_file=current.name,
        source_record=record,
        expected_source_identity=_SOURCE,
        available_at=_AVAILABLE,
    )


class OutcomeRevisionChainTests(unittest.TestCase):
    def test_initial_revision_is_self_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "current.json"
            current.write_text(
                json.dumps(_initial_record({"q": "win"}), sort_keys=True),
                encoding="utf-8",
            )
            chain = _verify(root, current)
            self.assertEqual(chain.revision_kind, "initial")
            self.assertEqual(chain.chain_length, 1)
            self.assertEqual(chain.root_revision_id, chain.revision_id)
            self.assertIsNone(chain.supersedes_revision_id)

    def test_valid_hash_bound_correction_chain_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = root / "v1.json"
            initial.write_text(
                json.dumps(_initial_record({"q": "win"}), sort_keys=True),
                encoding="utf-8",
            )
            current = root / "v2.json"
            current.write_text(
                json.dumps(_correction_record(root, {"q": "loss"}, initial), sort_keys=True),
                encoding="utf-8",
            )
            chain = _verify(root, current)
            initial_id = json.loads(initial.read_text(encoding="utf-8"))["revision"]["revision_id"]
            self.assertEqual(chain.revision_kind, "correction")
            self.assertEqual(chain.chain_length, 2)
            self.assertEqual(chain.root_revision_id, initial_id)
            self.assertEqual(chain.supersedes_revision_id, initial_id)

    def test_correction_requires_exact_predecessor_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = root / "v1.json"
            initial.write_text(
                json.dumps(_initial_record({"q": "win"}), sort_keys=True),
                encoding="utf-8",
            )
            current = root / "v2.json"
            current.write_text(
                json.dumps(
                    _correction_record(root, {"q": "loss"}, initial, supersedes="f" * 64),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "supersedes_revision_id"):
                _verify(root, current)

    def test_correction_rejects_predecessor_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = root / "v1.json"
            initial.write_text(
                json.dumps(_initial_record({"q": "win"}), sort_keys=True),
                encoding="utf-8",
            )
            current_record = _correction_record(root, {"q": "loss"}, initial)
            current_record["revision"]["predecessor_record_sha256"] = "0" * 64
            current = root / "v2.json"
            current.write_text(json.dumps(current_record, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not match predecessor"):
                _verify(root, current)

    def test_correction_must_be_strictly_later_than_predecessor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = root / "v1.json"
            initial.write_text(
                json.dumps(_initial_record({"q": "win"}), sort_keys=True),
                encoding="utf-8",
            )
            current = root / "v2.json"
            current.write_text(
                json.dumps(
                    _correction_record(
                        root,
                        {"q": "loss"},
                        initial,
                        effective_at="2026-01-01T11:00:00Z",
                    ),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "strictly after"):
                _verify(root, current)

    def test_noop_correction_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = root / "v1.json"
            initial.write_text(
                json.dumps(_initial_record({"q": "win"}), sort_keys=True),
                encoding="utf-8",
            )
            current = root / "v2.json"
            current.write_text(
                json.dumps(_correction_record(root, {"q": "win"}, initial), sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "change at least one outcome"):
                _verify(root, current)

    def test_correction_cannot_change_quote_identity_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = root / "v1.json"
            initial.write_text(
                json.dumps(_initial_record({"q": "win"}), sort_keys=True),
                encoding="utf-8",
            )
            current = root / "v2.json"
            current.write_text(
                json.dumps(_correction_record(root, {"other": "loss"}, initial), sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "preserve predecessor quote_outcomes key set"):
                _verify(root, current)

    def test_predecessor_source_identity_must_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial = root / "v1.json"
            initial_record = _initial_record({"q": "win"})
            initial_record["source"] = "official-results:other"
            initial.write_text(json.dumps(initial_record, sort_keys=True), encoding="utf-8")
            predecessor_id = initial_record["revision"]["revision_id"]
            predecessor_sha = _sha(initial)
            current_record = {
                "source": _SOURCE,
                "quote_outcomes": {"q": "loss"},
                "revision": {
                    "schema_version": 1,
                    "kind": "correction",
                    "effective_at": "2026-01-01T11:30:00Z",
                    "supersedes_revision_id": predecessor_id,
                    "predecessor_record_file": initial.name,
                    "predecessor_record_sha256": predecessor_sha,
                    "revision_id": canonical_outcome_revision_id(
                        source_identity=_SOURCE,
                        kind="correction",
                        effective_at="2026-01-01T11:30:00Z",
                        quote_outcomes={"q": "loss"},
                        supersedes_revision_id=predecessor_id,
                        predecessor_record_sha256=predecessor_sha,
                    ),
                },
            }
            current = root / "v2.json"
            current.write_text(json.dumps(current_record, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "predecessor source"):
                _verify(root, current)

    def test_revision_effective_time_cannot_postdate_source_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "current.json"
            current.write_text(
                json.dumps(
                    _initial_record(
                        {"q": "win"},
                        effective_at="2026-01-01T12:00:01Z",
                    ),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "must not be after outcome source available_at"):
                _verify(root, current)

    def test_revision_id_is_timezone_canonical_and_idempotent(self):
        outcomes = {"q": "win"}
        first = canonical_outcome_revision_id(
            source_identity=_SOURCE,
            kind="initial",
            effective_at="2026-01-01T11:00:00Z",
            quote_outcomes=outcomes,
        )
        same_instant = canonical_outcome_revision_id(
            source_identity=_SOURCE,
            kind="initial",
            effective_at="2026-01-01T12:00:00+01:00",
            quote_outcomes=outcomes,
        )
        self.assertEqual(first, same_instant)


if __name__ == "__main__":
    unittest.main()
