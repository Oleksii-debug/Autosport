import json
import tempfile
import unittest
from pathlib import Path

from autosport.run_registry import RunRegistry


class RunRegistryIdentityIntegrityTests(unittest.TestCase):
    def test_inconsistent_persisted_base_identity_fails_closed_before_silent_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            registry = RunRegistry(path)
            key = registry.begin("a" * 64, "b" * 64, "strategy", "run-1")
            registry.complete(key)

            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["runs"][key]["base_identity"] = "0" * 64
            path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "inconsistent base identity"):
                registry.begin("a" * 64, "b" * 64, "strategy", "run-2")

    def test_coherently_changed_identity_cannot_hide_behind_stale_experiment_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            registry = RunRegistry(path)
            key = registry.begin("a" * 64, "b" * 64, "strategy", "run-1")
            registry.complete(key)

            raw = json.loads(path.read_text(encoding="utf-8"))
            item = raw["runs"][key]
            item["market_sha256"] = "c" * 64
            item["base_identity"] = registry.experiment_identity(
                item["market_sha256"],
                item["results_sha256"],
                item["strategy_id"],
            )
            path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "inconsistent experiment key"):
                registry.begin("a" * 64, "b" * 64, "strategy", "run-2")


if __name__ == "__main__":
    unittest.main()
