import unittest

from autosport.evidence import EvidenceItem


class EvidenceFutureKeyIntegrityTests(unittest.TestCase):
    def test_rejects_future_result_key_nested_in_tuple(self):
        with self.assertRaisesRegex(ValueError, "future-result fields"):
            EvidenceItem(
                evidence_id="evidence-1",
                as_of_ts="2026-01-01T00:00:00+00:00",
                source="test-source",
                kind="research",
                payload={"nested": ({"winner": "selection-a"},)},
            )

    def test_allows_non_future_data_nested_in_tuple(self):
        item = EvidenceItem(
            evidence_id="evidence-2",
            as_of_ts="2026-01-01T00:00:00+00:00",
            source="test-source",
            kind="research",
            payload={"nested": ({"participant": "selection-a"},)},
        )

        self.assertEqual(item.payload["nested"][0]["participant"], "selection-a")

    def test_payload_is_defensively_snapshotted_before_validation_boundary(self):
        original = {"nested": [{"participant": "selection-a"}]}
        item = EvidenceItem(
            evidence_id="evidence-3",
            as_of_ts="2026-01-01T00:00:00+00:00",
            source="test-source",
            kind="research",
            payload=original,
        )
        canonical_hash = item.canonical_hash

        original["winner"] = "selection-b"
        original["nested"][0]["result"] = "selection-b"

        self.assertNotIn("winner", item.payload)
        self.assertNotIn("result", item.payload["nested"][0])
        self.assertEqual(item.canonical_hash, canonical_hash)

    def test_validated_payload_cannot_be_mutated_to_inject_future_result(self):
        item = EvidenceItem(
            evidence_id="evidence-4",
            as_of_ts="2026-01-01T00:00:00+00:00",
            source="test-source",
            kind="research",
            payload={"nested": [{"participant": "selection-a"}]},
        )
        canonical_hash = item.canonical_hash

        with self.assertRaisesRegex(TypeError, "EvidenceItem payload is immutable"):
            item.payload["winner"] = "selection-b"
        with self.assertRaisesRegex(TypeError, "EvidenceItem payload is immutable"):
            item.payload["nested"][0]["result"] = "selection-b"

        self.assertEqual(item.canonical_hash, canonical_hash)


if __name__ == "__main__":
    unittest.main()
