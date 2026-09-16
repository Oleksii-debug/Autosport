from __future__ import annotations

import importlib.util
from pathlib import Path
import tomllib

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_BINDER = _ROOT / "scripts" / "guarded_pyinstaller_bind.py"
_PYPROJECT = _ROOT / "pyproject.toml"
_EXPECTED_VERSION = "6.22.3"


def _load_binder_module():
    spec = importlib.util.spec_from_file_location("guarded_pyinstaller_bind", _BINDER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_build_extra_pins_validated_pyinstaller_version() -> None:
    project = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    assert project["project"]["optional-dependencies"]["build"] == [
        f"pyinstaller=={_EXPECTED_VERSION}"
    ]


def test_guard_runtime_version_contract_matches_build_pin() -> None:
    binder = _load_binder_module()
    assert binder._EXPECTED_PYINSTALLER_VERSION == _EXPECTED_VERSION
    binder._require_expected_pyinstaller_version(_EXPECTED_VERSION)

    with pytest.raises(RuntimeError, match="producer version mismatch"):
        binder._require_expected_pyinstaller_version("6.22.4")
