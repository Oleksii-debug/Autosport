import unittest

from autosport.outcome_provenance import (
    canonical_outcomes_sha256,
    validate_outcome_provenance,
)


def _results(**provenance_overrides):
    outcomes = {"tt-001|winner|alice": "win"}
    provenance = {
        "schema_version": 1,
        "kind": "historical_outcome_provenance",
        "source_identity": "official-results-feed:table-tennis:2026-01-01",
        "source_reference": "official-results-export-2026-01-01",
        "authority_reference": "result-authority-record-2026-01",
        "terms_reference": "result-feed-contract-2026",
        "retention_basis": "licensed internal research retention through 2027-01-01",
        "redistribution_policy": "internal_only",
        "licensing_or_retention_verified": True,
        "redistribution_verified": False,
        "authoritative_outcomes_verified": True,
        "acquired_at": "2026-01-01T11:05:00+00:00",
        "verified_at": "2026-01-01T11:06:00+00:00",
        "source_payload_sha256": "a" * 64,
        "quote_outcomes_sha256": canonical_outcomes_sha256(outcomes),
        "real_money_execution": False,
    }
    provenance.update(provenance_overrides)
    return {
        "schema_version": 1,
        "outcome_reveal_after": "2026-01-01T11:00:00+00:00",
        "quote_outcomes": outcomes,
        "outcome_provenance": provenance,
    }


class OutcomeProvenanceTests(unittest.TestCase):
    def test_accepts_content_bound_authoritative_provenance(self):
        report = validate_outcome_provenance(
            _results(),
            outcome_reveal_after="2026-01-01T11:00:00+00:00",
            dataset_imported_at="2026-01-01T11:10:00+00:00",
        )
        self.assertEqual(report.source_identity, "official-results-feed:table-tennis:2026-01-01")
        self.assertEqual(report.quote_outcomes_sha256, canonical_outcomes_sha256(_results()["quote_outcomes"]))

    def test_missing_provenance_fails_closed(self):
        results = _results()
        results.pop("outcome_provenance")
        with self.assertRaisesRegex(ValueError, "require outcome_provenance"):
            validate_outcome_provenance(
                results,
                outcome_reveal_after="2026-01-01T11:00:00+00:00",
                dataset_imported_at="2026-01-01T11:10:00+00:00",
            )

    def test_outcomes_hash_mismatch_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "quote_outcomes_sha256 mismatch"):
            validate_outcome_provenance(
                _results(quote_outcomes_sha256="b" * 64),
                outcome_reveal_after="2026-01-01T11:00:00+00:00",
                dataset_imported_at="2026-01-01T11:10:00+00:00",
            )

    def test_outcome_source_acquisition_before_reveal_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "acquired_at must not precede"):
            validate_outcome_provenance(
                _results(acquired_at="2026-01-01T10:59:59+00:00"),
                outcome_reveal_after="2026-01-01T11:00:00+00:00",
                dataset_imported_at="2026-01-01T11:10:00+00:00",
            )

    def test_import_before_outcome_verification_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "imported_at must not precede"):
            validate_outcome_provenance(
                _results(),
                outcome_reveal_after="2026-01-01T11:00:00+00:00",
                dataset_imported_at="2026-01-01T11:05:30+00:00",
            )

    def test_unverified_authority_or_rights_fail_closed(self):
        for field in ("authoritative_outcomes_verified", "licensing_or_retention_verified"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    validate_outcome_provenance(
                        _results(**{field: False}),
                        outcome_reveal_after="2026-01-01T11:00:00+00:00",
                        dataset_imported_at="2026-01-01T11:10:00+00:00",
                    )

    def test_permitted_redistribution_requires_explicit_verification(self):
        with self.assertRaisesRegex(ValueError, "redistribution_verified=true"):
            validate_outcome_provenance(
                _results(redistribution_policy="permitted", redistribution_verified=False),
                outcome_reveal_after="2026-01-01T11:00:00+00:00",
                dataset_imported_at="2026-01-01T11:10:00+00:00",
            )


if __name__ == "__main__":
    unittest.main()
