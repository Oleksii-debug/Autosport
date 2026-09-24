from __future__ import annotations

import importlib.metadata
import tomllib
import unittest
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _normalized_distribution_name(value: str) -> str:
    return value.lower().replace("_", "-").replace(".", "-")


class ConsoleScriptEntrypointManifestTests(unittest.TestCase):
    def _declared_scripts(self) -> dict[str, str]:
        project = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]
        scripts = project["scripts"]
        self.assertIsInstance(scripts, dict)
        return dict(scripts)

    def _installed_scripts(self) -> dict[str, importlib.metadata.EntryPoint]:
        scripts: dict[str, importlib.metadata.EntryPoint] = {}
        for entry in importlib.metadata.entry_points(group="console_scripts"):
            distribution = entry.dist
            if distribution is None:
                continue
            name = distribution.metadata.get("Name")
            if not isinstance(name, str):
                continue
            if _normalized_distribution_name(name) != "autosport-lab":
                continue
            self.assertNotIn(entry.name, scripts)
            scripts[entry.name] = entry
        return scripts

    def test_installed_console_script_manifest_exactly_matches_pyproject(self) -> None:
        declared = self._declared_scripts()
        installed = self._installed_scripts()

        self.assertEqual(set(installed), set(declared))
        for name, target in declared.items():
            with self.subTest(name=name):
                self.assertEqual(installed[name].value, target)

    def test_every_declared_console_script_target_loads_as_exact_callable(self) -> None:
        declared = self._declared_scripts()
        installed = self._installed_scripts()

        for name, target in declared.items():
            with self.subTest(name=name):
                module_name, function_name = target.split(":", 1)
                loaded = installed[name].load()
                self.assertTrue(callable(loaded))
                self.assertEqual(loaded.__module__, module_name)
                self.assertEqual(loaded.__name__, function_name)


if __name__ == "__main__":
    unittest.main()
