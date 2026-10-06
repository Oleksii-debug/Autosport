from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CanonicalTestRunnerContractTests(unittest.TestCase):
    def test_pyproject_declares_pytest_test_extra(self) -> None:
        payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        test_dependencies = payload["project"]["optional-dependencies"]["test"]
        self.assertTrue(
            any(str(item).startswith("pytest") for item in test_dependencies),
            "the canonical test extra must install pytest so function-style tests are executable",
        )

    def test_ci_executes_pytest_not_unittest_only_discovery(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("python -m pip install -e '.[test]'", workflow)
        self.assertIn("python -m pytest -v tests", workflow)
        self.assertNotIn("python -m unittest discover -s tests", workflow)


if __name__ == "__main__":
    unittest.main()
