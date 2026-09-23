from __future__ import annotations

import importlib.metadata
import unittest


class PackagingEntrypointTests(unittest.TestCase):
    def _scripts(self):
        return {
            entry.name: entry
            for entry in importlib.metadata.entry_points(group="console_scripts")
            if entry.dist is not None and entry.dist.metadata.get("Name") == "autosport-lab"
        }

    def test_historical_acquisition_cli_is_installed_and_resolvable(self) -> None:
        scripts = self._scripts()
        self.assertIn("autosport-acquire-historical-evidence", scripts)
        entry = scripts["autosport-acquire-historical-evidence"]
        self.assertEqual(entry.value, "autosport.historical_acquisition:main")
        loaded = entry.load()
        self.assertEqual(loaded.__module__, "autosport.historical_acquisition")
        self.assertEqual(loaded.__name__, "main")

    def test_historical_corpus_clis_route_through_governance_binding(self) -> None:
        scripts = self._scripts()
        expected = {
            "autosport-build-historical-corpus": "autosport.historical_governance:corpus_main",
            "autosport-build-historical-corpus-from-bundle": "autosport.historical_governance:bundle_corpus_main",
        }
        for name, target in expected.items():
            with self.subTest(name=name):
                self.assertIn(name, scripts)
                entry = scripts[name]
                self.assertEqual(entry.value, target)
                loaded = entry.load()
                module_name, function_name = target.split(":", 1)
                self.assertEqual(loaded.__module__, module_name)
                self.assertEqual(loaded.__name__, function_name)


if __name__ == "__main__":
    unittest.main()