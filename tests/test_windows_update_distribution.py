"""Distribution contract for the non-authorizing update diagnostic.

This checks the actual project metadata rather than merely importing the
source module; missing console entrypoints otherwise escape source-only tests.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path


def test_update_diagnostic_distribution_contract():
    project = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["scripts"]["autosport-update-diagnostic"] == (
        "autosport.windows_update_cli:main"
    )
    assert "cryptography>=44,<47" in metadata["project"]["optional-dependencies"][
        "update-diagnostic"
    ]


def test_update_diagnostic_module_rejects_missing_request_without_install(tmp_path):
    project = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (
        str(project / "src"), env.get("PYTHONPATH", ""),
    )))
    result = subprocess.run(
        [sys.executable, "-m", "autosport.windows_update_cli", str(tmp_path / "відсутній запит.json")],
        capture_output=True, text=True, timeout=15, env=env,
    )
    assert result.returncode == 2
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report == {
        "status": "DIAGNOSTIC_REJECTED",
        "install_authorized": False,
        "independent_trust_root_verified": False,
        "human_tested": False,
        "nvda_verified": False,
    }
    assert str(tmp_path) not in result.stdout
