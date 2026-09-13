from __future__ import annotations

from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "windows-build.yml"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "walk_forward_package_smoke.ps1"


class WindowsWalkForwardArtifactContractTests(unittest.TestCase):
    def test_governed_walk_forward_artifact_publishes_referenced_dataset(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("dist/walk-forward-package-smoke.json", workflow)
        self.assertIn("dist/walk-forward-package-smoke-dataset/**", workflow)
        self.assertIn("dist/walk-forward-package-smoke-report.json", workflow)
        self.assertIn(
            "dist/fresh-extraction-walk-forward-package-smoke-report.json",
            workflow,
        )

    def test_bundle_and_uploaded_dataset_directory_share_canonical_name(self) -> None:
        script = SMOKE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "$datasetRoot = Join-Path $PWD 'dist/walk-forward-package-smoke-dataset'",
            script,
        )
        self.assertIn('"path": dataset_root.name', script)


if __name__ == "__main__":
    unittest.main()
