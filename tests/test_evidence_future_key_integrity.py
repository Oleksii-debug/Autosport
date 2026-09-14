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


if __name__ == "__main__":
    unittest.main()
