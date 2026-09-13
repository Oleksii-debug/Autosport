import unittest
from decimal import Decimal

from autosport.research_pipeline import ResearchDecisionPolicy, ResearchEvidence
from autosport.research_strategy import _evidence_from_dict


class ResearchQualityFlagCanonicalityTests(unittest.TestCase):
    @staticmethod
    def _evidence(quality_flags):
        return {
            "evidence_id": "quality-flag-canonicality",
            "quote_key": "event|winner|selection",
            "source_id": "sealed-test-source",
            "observed_at": "2026-09-12T09:00:00+00:00",
            "available_at": "2026-09-12T09:00:00+00:00",
            "decimal_odds": "1.80",
            "content_sha256": "0" * 64,
            "quality_flags": quality_flags,
        }

    @staticmethod
    def _typed_evidence(quality_flags):
        return ResearchEvidence(
            evidence_id="typed-quality-flag-canonicality",
            quote_key="event|winner|selection",
            source_id="sealed-test-source",
            observed_at="2026-09-12T09:00:00+00:00",
            available_at="2026-09-12T09:00:00+00:00",
            decimal_odds=Decimal("1.80"),
            content_sha256="0" * 64,
            quality_flags=quality_flags,
        )

    def test_padded_blocked_quality_flag_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "non-empty canonical strings"):
            _evidence_from_dict(self._evidence([" GAP_DETECTED "]))

    def test_canonical_blocked_quality_flag_is_preserved(self):
        evidence = _evidence_from_dict(self._evidence(["GAP_DETECTED"]))
        self.assertEqual(evidence.quality_flags, ("GAP_DETECTED",))

    def test_typed_evidence_rejects_padded_flag_without_parser(self):
        with self.assertRaisesRegex(ValueError, "non-empty canonical strings"):
            self._typed_evidence((" GAP_DETECTED ",))

    def test_typed_evidence_rejects_non_string_flag_without_coercion(self):
        with self.assertRaisesRegex(ValueError, "non-empty canonical strings"):
            self._typed_evidence((123,))

    def test_typed_evidence_rejects_scalar_string_collection(self):
        with self.assertRaisesRegex(ValueError, "quality flags must be a tuple or list"):
            self._typed_evidence("BAD")

    def test_typed_evidence_accepts_canonical_list_and_normalizes_to_tuple(self):
        evidence = self._typed_evidence(["GAP_DETECTED"])
        self.assertEqual(evidence.quality_flags, ("GAP_DETECTED",))

    def test_policy_rejects_scalar_string_collection(self):
        with self.assertRaisesRegex(ValueError, "tuple, list, set, or frozenset"):
            ResearchDecisionPolicy(blocked_quality_flags="GAP_DETECTED")

    def test_policy_rejects_non_string_member_without_coercion(self):
        with self.assertRaisesRegex(ValueError, "non-empty canonical strings"):
            ResearchDecisionPolicy(blocked_quality_flags=[123])

    def test_policy_accepts_canonical_list_and_normalizes_to_frozenset(self):
        policy = ResearchDecisionPolicy(blocked_quality_flags=["GAP_DETECTED"])
        self.assertEqual(policy.blocked_quality_flags, frozenset({"GAP_DETECTED"}))


if __name__ == "__main__":
    unittest.main()
