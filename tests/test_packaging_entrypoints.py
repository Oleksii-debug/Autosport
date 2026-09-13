from __future__ import annotations

import importlib.metadata
import unittest


class PackagingEntrypointTests(unittest.TestCase):
    def test_historical_acquisition_cli_is_installed_and_resolvable(self) -> None:
        scripts = {
            entry.name: entry
            for entry in importlib.metadata.entry_points(group="console_scripts")
            if entry.dist is not None and entry.dist.metadata.get("Name") == "autosport-lab"
        }
        self.assertIn("autosport-acquire-historical-evidence", scripts)
        entry = scripts["autosport-acquire-historical-evidence"]
        self.assertEqual(entry.value, "autosport.historical_acquisition:main")
        loaded = entry.load()
        self.assertEqual(loaded.__module__, "autosport.historical_acquisition")
        self.assertEqual(loaded.__name__, "main")


if __name__ == "__main__":
    unittest.main()