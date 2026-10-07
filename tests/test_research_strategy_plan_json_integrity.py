import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.research_strategy import ResearchStrategyPlan


_PACKAGED_PLAN = Path("examples/tt_demo/research_plan.json")


class ResearchStrategyPlanJsonIntegrityTests(unittest.TestCase):
    def _write_plan(self, root: Path, content: str) -> Path:
        path = root / "research-plan.json"
        path.write_text(content, encoding="utf-8")
        return path

    def test_rejects_duplicate_keys_inside_nested_plan_object(self):
        source = _PACKAGED_PLAN.read_text(encoding="utf-8")
        marker = '          "probability": "0.60",'
        duplicate = source.replace(
            marker,
            marker + '\n          "probability": "0.61",',
            1,
        )
        self.assertNotEqual(duplicate, source)

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_plan(Path(tmp), duplicate)
            with self.assertRaises(ValueError) as caught:
                ResearchStrategyPlan.from_path(path)

        self.assertIn("duplicate JSON key 'probability'", str(caught.exception))

    def test_rejects_non_finite_constants_even_in_ignored_root_field(self):
        source = _PACKAGED_PLAN.read_text(encoding="utf-8")
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token), tempfile.TemporaryDirectory() as tmp:
                poisoned = source.replace(
                    "{\n",
                    '{\n  "ignored_non_finite": ' + token + ",\n",
                    1,
                )
                path = self._write_plan(Path(tmp), poisoned)
                with self.assertRaises(ValueError) as caught:
                    ResearchStrategyPlan.from_path(path)
                self.assertIn(
                    f"non-finite JSON value {token!r}",
                    str(caught.exception),
                )

    def test_rejects_numeric_overflow_to_non_finite_before_hashing(self):
        source = _PACKAGED_PLAN.read_text(encoding="utf-8")
        for token in ("1e400", "-1e400"):
            with self.subTest(token=token), tempfile.TemporaryDirectory() as tmp:
                poisoned = source.replace(
                    "{\n",
                    '{\n  "ignored_numeric_overflow": ' + token + ",\n",
                    1,
                )
                path = self._write_plan(Path(tmp), poisoned)
                with self.assertRaisesRegex(ValueError, "non-finite JSON number"):
                    ResearchStrategyPlan.from_path(path)

    def test_rejects_lone_surrogate_value_before_hashing(self):
        source = _PACKAGED_PLAN.read_text(encoding="utf-8")
        poisoned = source.replace(
            "{\n",
            '{\n  "ignored_surrogate": "\\ud800",\n',
            1,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_plan(Path(tmp), poisoned)
            with self.assertRaisesRegex(ValueError, "non-UTF-8 JSON text"):
                ResearchStrategyPlan.from_path(path)

    def test_rejects_lone_surrogate_object_key_before_hashing(self):
        source = _PACKAGED_PLAN.read_text(encoding="utf-8")
        poisoned = source.replace(
            "{\n",
            '{\n  "\\ud800": 1,\n',
            1,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_plan(Path(tmp), poisoned)
            with self.assertRaisesRegex(ValueError, "non-UTF-8 JSON text"):
                ResearchStrategyPlan.from_path(path)

    def test_rejects_excessive_json_nesting_with_domain_error(self):
        source = _PACKAGED_PLAN.read_text(encoding="utf-8")
        nested = "[" * 70 + "0" + "]" * 70
        poisoned = source.replace(
            "{\n",
            '{\n  "ignored_nested": ' + nested + ",\n",
            1,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_plan(Path(tmp), poisoned)
            with self.assertRaisesRegex(ValueError, "JSON nesting exceeds supported depth"):
                ResearchStrategyPlan.from_path(path)

    def test_rejects_boolean_schema_version_alias(self):
        source = _PACKAGED_PLAN.read_text(encoding="utf-8")
        poisoned = source.replace('"schema_version": 1', '"schema_version": true', 1)
        self.assertNotEqual(poisoned, source)

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_plan(Path(tmp), poisoned)
            with self.assertRaisesRegex(ValueError, "schema_version must be integer 1"):
                ResearchStrategyPlan.from_path(path)

    def test_valid_packaged_plan_preserves_canonical_digest(self):
        raw = json.loads(_PACKAGED_PLAN.read_text(encoding="utf-8"))
        canonical = json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        plan = ResearchStrategyPlan.from_path(_PACKAGED_PLAN)

        self.assertEqual(plan.source_sha256, expected)


    def test_direct_plan_constructor_rejects_uppercase_source_digest_alias(self):
        plan = ResearchStrategyPlan.from_path(_PACKAGED_PLAN)

        with self.assertRaisesRegex(
            ValueError,
            "lowercase SHA-256 hex digest",
        ):
            ResearchStrategyPlan(
                plan.instructions,
                plan.source_sha256.upper(),
            )


    def test_plan_rejects_numeric_decision_identity_instead_of_string_coercion(self):
        raw = json.loads(_PACKAGED_PLAN.read_text(encoding="utf-8"))
        raw["decisions"][0]["decision_id"] = 123

        with self.assertRaisesRegex(
            ValueError,
            "research decision_id must be non-empty canonical text",
        ):
            ResearchStrategyPlan.from_dict(raw)

    def test_plan_rejects_noncanonical_whitespace_decision_identity(self):
        raw = json.loads(_PACKAGED_PLAN.read_text(encoding="utf-8"))
        raw["decisions"][0]["decision_id"] = " research-demo-1 "

        with self.assertRaisesRegex(
            ValueError,
            "research decision_id must be non-empty canonical text",
        ):
            ResearchStrategyPlan.from_dict(raw)

    def test_plan_rejects_numeric_trigger_identity_instead_of_string_coercion(self):
        raw = json.loads(_PACKAGED_PLAN.read_text(encoding="utf-8"))
        raw["decisions"][0]["trigger_quote_key"] = 123

        with self.assertRaisesRegex(
            ValueError,
            "research trigger_quote_key must be non-empty canonical text",
        ):
            ResearchStrategyPlan.from_dict(raw)


    def test_from_dict_rejects_uppercase_explicit_source_digest_alias(self):
        raw = json.loads(_PACKAGED_PLAN.read_text(encoding="utf-8"))
        canonical = ResearchStrategyPlan.from_dict(raw)

        with self.assertRaisesRegex(
            ValueError,
            "lowercase SHA-256 hex digest",
        ):
            ResearchStrategyPlan.from_dict(
                raw,
                source_sha256=canonical.source_sha256.upper(),
            )


    def test_plan_rejects_numeric_forecast_identity_and_timestamp_coercion(self):
        for field, value in (
            ("model_id", 123),
            ("forecast_id", True),
            ("generated_at", 123),
            ("market_snapshot_hash", 123),
        ):
            with self.subTest(field=field):
                raw = json.loads(_PACKAGED_PLAN.read_text(encoding="utf-8"))
                raw["decisions"][0]["forecasts"][0][field] = value
                with self.assertRaisesRegex(
                    ValueError,
                    "ForecastRecord .* must be non-empty canonical text",
                ):
                    ResearchStrategyPlan.from_dict(raw)

    def test_plan_rejects_nonobject_forecast_provenance(self):
        raw = json.loads(_PACKAGED_PLAN.read_text(encoding="utf-8"))
        raw["decisions"][0]["forecasts"][0]["provenance"] = [
            ["source", "coerced"]
        ]

        with self.assertRaisesRegex(
            ValueError,
            "ForecastRecord provenance must be a JSON object",
        ):
            ResearchStrategyPlan.from_dict(raw)

    def test_plan_rejects_numeric_research_evidence_identity_and_hash_coercion(self):
        for field, value in (
            ("evidence_id", 123),
            ("source_id", True),
            ("observed_at", 123),
            ("content_sha256", 123),
            ("market_snapshot_hash", 123),
        ):
            with self.subTest(field=field):
                raw = json.loads(_PACKAGED_PLAN.read_text(encoding="utf-8"))
                raw["decisions"][0]["evidence"][0][field] = value
                with self.assertRaisesRegex(
                    ValueError,
                    "ResearchEvidence .* must be non-empty canonical text",
                ):
                    ResearchStrategyPlan.from_dict(raw)

    def test_plan_rejects_numeric_risk_evidence_provenance_coercion(self):
        raw = json.loads(_PACKAGED_PLAN.read_text(encoding="utf-8"))
        risk = raw["decisions"][0].get("risk_of_ruin_evidence")
        if risk is None:
            self.skipTest("packaged research plan has no risk_of_ruin_evidence")
        for field, value in (
            ("evidence_id", 123),
            ("producer_identity", True),
            ("causal_cutoff", 123),
            ("base_portfolio_sha256", 123),
        ):
            with self.subTest(field=field):
                mutated = json.loads(_PACKAGED_PLAN.read_text(encoding="utf-8"))
                mutated["decisions"][0]["risk_of_ruin_evidence"][field] = value
                with self.assertRaisesRegex(
                    ValueError,
                    "research risk .* must be non-empty canonical text",
                ):
                    ResearchStrategyPlan.from_dict(mutated)


    def test_plan_normalizes_missing_forecast_fields_to_value_error(self):
        raw = json.loads(_PACKAGED_PLAN.read_text(encoding="utf-8"))
        del raw["decisions"][0]["forecasts"][0]["forecast_id"]

        with self.assertRaisesRegex(
            ValueError,
            "ForecastRecord is missing required field\(s\): forecast_id",
        ):
            ResearchStrategyPlan.from_dict(raw)

    def test_plan_normalizes_missing_evidence_fields_to_value_error(self):
        raw = json.loads(_PACKAGED_PLAN.read_text(encoding="utf-8"))
        del raw["decisions"][0]["evidence"][0]["content_sha256"]

        with self.assertRaisesRegex(
            ValueError,
            "ResearchEvidence is missing required field\(s\): content_sha256",
        ):
            ResearchStrategyPlan.from_dict(raw)

    def test_plan_rejects_nonobject_and_missing_key_scenario_outcomes(self):
        raw = json.loads(_PACKAGED_PLAN.read_text(encoding="utf-8"))
        raw["decisions"][0]["scenario_groups"][0]["outcomes"][0] = 123
        with self.assertRaisesRegex(
            ValueError,
            "research scenario outcome must be an object",
        ):
            ResearchStrategyPlan.from_dict(raw)

        raw = json.loads(_PACKAGED_PLAN.read_text(encoding="utf-8"))
        del raw["decisions"][0]["scenario_groups"][0]["outcomes"][0]["quote_key"]
        with self.assertRaisesRegex(
            ValueError,
            "research scenario outcome is missing required field quote_key",
        ):
            ResearchStrategyPlan.from_dict(raw)


if __name__ == "__main__":
    unittest.main()
