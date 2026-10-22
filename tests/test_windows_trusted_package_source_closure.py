"""Regression for the exact-Git Windows packager's isolated import graph.

The Windows builder intentionally stages only a small allowlisted source tree.
A newly imported module must be in the source SHA oracle, Git archive and
isolated loader, or actual Windows builds fail after the expensive EXE work.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


_BUILD = Path(__file__).resolve().parents[1] / "scripts" / "build_windows.ps1"
_ATTRIBUTION = "src/autosport/release_binary_attribution.py"


def _source_contract():
    source = _BUILD.read_text(encoding="utf-8")

    launcher_match = re.search(
        r"(?ms)^\$trustedPackageLauncher = @'\r?\n(.*?)\r?\n'@$",
        source,
    )
    if launcher_match is None:
        raise AssertionError("isolated exact-source package launcher is missing")
    parsed = ast.parse(launcher_match.group(1))
    required_nodes = [
        statement.value
        for statement in parsed.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "required"
            for target in statement.targets
        )
    ]
    if len(required_nodes) != 1:
        raise AssertionError("trusted package launcher needs one required set")
    required = ast.literal_eval(required_nodes[0])

    paths_match = re.search(
        r"(?ms)^\$trustedPackagePathsJson = ConvertTo-Json -Compress -InputObject @\(\r?\n(.*?)\r?\n\)",
        source,
    )
    if paths_match is None:
        raise AssertionError("exact-Git source path manifest is missing")
    path_lines = paths_match.group(1).splitlines()
    manifest_paths = [
        re.fullmatch(r"\s*'([^']+)'\s*,?\s*", line).group(1)
        for line in path_lines
    ]

    archive_match = re.search(
        r"(?m)^& \$gitExecutable archive --format=zip .*$",
        source,
    )
    if archive_match is None:
        raise AssertionError("exact-Git archive call is missing")
    archive_paths = re.findall(
        r"(?:scripts|src)/[A-Za-z0-9_./-]+\.py",
        archive_match.group(0),
    )

    loads = [
        tuple(ast.literal_eval(argument) for argument in statement.value.args)
        for statement in parsed.body
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "load_module"
    ]
    return required, manifest_paths, archive_paths, loads


class WindowsTrustedPackageSourceClosure(unittest.TestCase):
    def test_binary_attribution_is_trusted_exact_source_member_everywhere(self):
        required, manifest, archive, loads = _source_contract()
        self.assertIn(_ATTRIBUTION, required)
        self.assertEqual(set(manifest), required)
        self.assertEqual(set(archive), required)
        self.assertEqual(len(manifest), len(required))
        self.assertEqual(len(archive), len(required))
        self.assertEqual({path for _, path in loads}, required - {"scripts/package_windows.py"})

    def test_attribution_loaded_before_release_package_import(self):
        required, manifest, archive, loads = _source_contract()
        names = [name for name, _ in loads]
        self.assertIn("autosport.release_binary_attribution", names)
        self.assertLess(
            names.index("autosport.release_binary_attribution"),
            names.index("autosport.release_package"),
        )
        self.assertTrue((_BUILD.parents[1] / _ATTRIBUTION).is_file())


if __name__ == "__main__":
    unittest.main()
