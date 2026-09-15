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


if __name__ == "__main__":
    unittest.main()
