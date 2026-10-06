import unittest

from autosport.evidence import EvidenceItem


class EvidenceFutureKeyIntegrityTests(unittest.TestCase):
    def test_rejects_future_result_keys_nested_in_tuple(self):
        for key in ("result", "winner", "outcome"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "future-result fields"):
                    EvidenceItem(
                        evidence_id="evidence-1",
                        as_of_ts="2026-01-01T00:00:00+00:00",
                        source="test-source",
                        kind="research",
                        payload={"nested": ({key: "selection-a"},)},
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

    def test_validated_payload_is_structurally_immutable_even_via_builtin_base_methods(self):
        item = EvidenceItem(
            evidence_id="evidence-4",
            as_of_ts="2026-01-01T00:00:00+00:00",
            source="test-source",
            kind="research",
            payload={"nested": [{"participant": "selection-a"}]},
        )
        canonical_hash = item.canonical_hash

        self.assertNotIsInstance(item.payload, dict)
        self.assertNotIsInstance(item.payload["nested"][0], dict)
        self.assertIsInstance(item.payload["nested"], tuple)
        with self.assertRaises(TypeError):
            dict.__setitem__(item.payload, "winner", "selection-b")
        with self.assertRaises(TypeError):
            dict.__setitem__(item.payload["nested"][0], "result", "selection-b")
        with self.assertRaises(TypeError):
            list.append(item.payload["nested"], {"result": "selection-b"})

        self.assertNotIn("winner", item.payload)
        self.assertNotIn("result", item.payload["nested"][0])
        self.assertEqual(item.canonical_hash, canonical_hash)


if __name__ == "__main__":
    unittest.main()
