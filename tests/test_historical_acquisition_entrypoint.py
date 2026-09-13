import importlib.metadata
import unittest


class HistoricalAcquisitionEntrypointTests(unittest.TestCase):
    def test_installed_console_entrypoint_routes_to_canonical_acquisition_module(self) -> None:
        distribution = importlib.metadata.distribution("autosport-lab")
        console_scripts = {
            entry.name: entry.value
            for entry in distribution.entry_points
            if entry.group == "console_scripts"
        }
        self.assertEqual(
            console_scripts.get("autosport-acquire-historical-evidence"),
            "autosport.historical_acquisition:main",
        )


if __name__ == "__main__":
    unittest.main()
