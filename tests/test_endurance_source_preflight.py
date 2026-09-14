from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class EnduranceSourcePreflightTests(unittest.TestCase):
    def test_endurance_workflow_checks_out_and_preflights_exact_candidate_before_install(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "endurance.yml").read_text(encoding="utf-8")
        source_env = "AUTOSPORT_SOURCE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}"
        exact_ref = "ref: ${{ github.event.pull_request.head.sha || github.sha }}"
        preflight = (
            'python scripts/verify_source_checkout.py --source-sha '
            '"${{ env.AUTOSPORT_SOURCE_SHA }}"'
        )
        install = "run: python -m pip install -e ."
        endurance = "run: python -m autosport endurance"

        self.assertIn(source_env, workflow)
        self.assertIn(exact_ref, workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn(preflight, workflow)
        self.assertLess(workflow.index(preflight), workflow.index(install))
        self.assertLess(workflow.index(preflight), workflow.index(endurance))


if __name__ == "__main__":
    unittest.main()
