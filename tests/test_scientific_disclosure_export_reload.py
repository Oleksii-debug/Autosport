from __future__ import annotations

import subprocess
import sys


def test_registry_reload_cannot_reopen_direct_outcome_export() -> None:
    script = r'''
import importlib
import autosport.scientific_registry as registry
from autosport.scientific_disclosure_export import ScientificDisclosureExportError

for _ in range(2):
    registry = importlib.reload(registry)
    instance = object.__new__(registry.ScientificRegistry)
    try:
        instance.export_reproducibility_bundle("experiment", "outward.json")
    except ScientificDisclosureExportError:
        continue
    raise AssertionError("scientific registry reload reopened direct outcome export")
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
