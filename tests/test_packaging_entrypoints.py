from __future__ import annotations

import importlib
import tomllib
import unittest
from pathlib import Path


class PackagingEntrypointTests(unittest.TestCase):
    def test_historical_acquisition_cli_is_installed_and_resolvable(self) -> None:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with pyproject.open("rb") as stream:
            metadata = tomllib.load(stream)

        target = metadata["project"]["scripts"].get("autosport-acquire-historical-evidence")
        self.assertEqual(target, "autosport.historical_acquisition:main")

        module_name, attribute = target.split(":", 1)
        entrypoint = getattr(importlib.import_module(module_name), attribute)
        self.assertTrue(callable(entrypoint))


if __name__ == "__main__":
    unittest.main()
