import unittest
from types import MappingProxyType

from autosport.decision_ledger import DecisionRecord
from autosport.evidence import EvidenceItem
from autosport.forecasting import ForecastRecord


def _proxy_payload():
    nested_backing = {"feature": "serve-form"}
    top_backing = {"nested": [MappingProxyType(nested_backing)]}
    return MappingProxyType(top_backing), top_backing, nested_backing


class CausalMappingAliasIntegrityTests(unittest.TestCase):
    def test_evidence_snapshots_top_level_and_nested_generic_mappings(self):
        payload, top_backing, nested_backing = _proxy_payload()
        item = EvidenceItem(
            evidence_id="evidence-mapping-alias",
            as_of_ts="2026-01-01T00:00:00+00:00",
            source="test-source",
            kind="research",
            payload=payload,
        )
        canonical_hash = item.canonical_hash

        top_backing["winner"] = "selection-b"
        nested_backing["result"] = "selection-b"

        self.assertNotIn("winner", item.payload)
        self.assertNotIn("result", item.payload["nested"][0])
        self.assertEqual(item.canonical_hash, canonical_hash)

    def test_forecast_snapshots_top_level_and_nested_generic_mappings(self):
        provenance, top_backing, nested_backing = _proxy_payload()
        record = ForecastRecord(
            quote_key="event|winner|selection-a",
            probability="0.6",
            model_id="model-a",
            model_version="1",
            strategy_version="strategy-a",
            model_training_cutoff_ts="2026-01-01T00:00:00+00:00",
            input_cutoff_ts="2026-01-02T00:00:00+00:00",
            generated_at="2026-01-02T00:01:00+00:00",
            provenance=provenance,
            forecast_id="forecast-mapping-alias",
        )
        canonical_hash = record.canonical_hash

        top_backing["winner"] = "selection-b"
        nested_backing["outcome"] = "selection-b"

        self.assertNotIn("winner", record.provenance)
        self.assertNotIn("outcome", record.provenance["nested"][0])
        self.assertEqual(record.canonical_hash, canonical_hash)

    def test_decision_snapshots_top_level_and_nested_generic_mappings(self):
        payload, top_backing, nested_backing = _proxy_payload()
        record = DecisionRecord(
            replay_run_id="run-mapping-alias",
            agent="agent-a",
            observed_ts="2026-01-01T00:00:00+00:00",
            action="OBSERVE",
            payload=payload,
            context_hash="context-a",
            decision_id="decision-mapping-alias",
            recorded_at="2026-01-01T00:00:01+00:00",
        )
        exported = record.to_dict()

        top_backing["winner"] = "selection-b"
        nested_backing["future_quote"] = {"odds": "9.0"}

        self.assertNotIn("winner", record.payload)
        self.assertNotIn("future_quote", record.payload["nested"][0])
        self.assertEqual(record.to_dict(), exported)


if __name__ == "__main__":
    unittest.main()
