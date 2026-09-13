from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.historical_corpus as corpus


def _proof(*, verified_at: str) -> dict[str, object]:
    return {
        "verified_at": verified_at,
        "terms_reference": "https://example.test/terms",
    }


class GovernanceProofImportChronologyTests(unittest.TestCase):
    def test_future_governance_verification_fails_before_snapshot_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "corpus"
            with (
                patch.object(
                    corpus,
                    "_governance_proof",
                    return_value=_proof(verified_at="2026-01-02T12:00:00+00:00"),
                ),
                patch.object(
                    corpus,
                    "_snapshot",
                    side_effect=AssertionError("snapshot must not be read after impossible governance chronology"),
                ) as snapshot,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "imported_at must not precede governance proof verification",
                ):
                    corpus.assemble_historical_corpus(
                        [(root / "market.jsonl", root / "evidence.json")],
                        results_path=root / "results.json",
                        governance_proof_path=root / "governance.json",
                        output_dir=output,
                        name="blocked future governance proof",
                        outcome_reveal_after="2026-01-02T09:00:00+00:00",
                        imported_at="2026-01-02T10:00:00+00:00",
                    )

            snapshot.assert_not_called()
            self.assertFalse(output.exists())

    def test_equal_or_earlier_governance_verification_passes_chronology_gate(self) -> None:
        for verified_at in (
            "2026-01-02T10:00:00+00:00",
            "2026-01-02T09:59:59+00:00",
        ):
            with self.subTest(verified_at=verified_at), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                output = root / "corpus"
                with (
                    patch.object(
                        corpus,
                        "_governance_proof",
                        return_value=_proof(verified_at=verified_at),
                    ),
                    patch.object(
                        corpus,
                        "_snapshot",
                        side_effect=RuntimeError("downstream-sentinel"),
                    ) as snapshot,
                ):
                    with self.assertRaisesRegex(RuntimeError, "downstream-sentinel"):
                        corpus.assemble_historical_corpus(
                            [(root / "market.jsonl", root / "evidence.json")],
                            results_path=root / "results.json",
                            governance_proof_path=root / "governance.json",
                            output_dir=output,
                            name="allowed governance proof boundary",
                            outcome_reveal_after="2026-01-02T09:00:00+00:00",
                            imported_at="2026-01-02T10:00:00+00:00",
                        )

                snapshot.assert_called_once()
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
