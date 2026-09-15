from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_GUARDED_PYINSTALLER = _ROOT / "scripts" / "guarded_pyinstaller_bind.py"
_PRODUCER_TEST_PATH = _ROOT / "tests" / "test_windows_pyinstaller_producer_handoff.py"
_SPEC = importlib.util.spec_from_file_location(
    "_autosport_pyinstaller_producer_handoff_tests_preoracle",
    _PRODUCER_TEST_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
_PRODUCER_TESTS = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _PRODUCER_TESTS
_SPEC.loader.exec_module(_PRODUCER_TESTS)

_REAL_WINDOWS_PYINSTALLER = (
    os.name == "nt" and importlib.util.find_spec("PyInstaller") is not None
)


def test_expected_snapshot_replica_authentication_precedes_live_mutation() -> None:
    script = _GUARDED_PYINSTALLER.read_text(encoding="utf-8")

    primary_mutation = script.index("expected_mutator(snapshot)")
    primary_oracle = script.index(
        "oracle_stream, snapshot_identity = _open_expected_snapshot_oracle(",
        primary_mutation,
    )
    replica_materialization = script.index(
        "reference_snapshot, _ = _make_expected_snapshot(",
        primary_oracle,
    )
    replica_mutation = script.index("expected_mutator(reference_snapshot)", replica_materialization)
    replica_compare = script.index("if reference_digest != expected_digest:", replica_mutation)
    live_mutation = script.index("live_result = live_mutator()", replica_compare)

    assert (
        primary_mutation
        < primary_oracle
        < replica_materialization
        < replica_mutation
        < replica_compare
        < live_mutation
    )
    assert "AUTOSPORT_TEST_WRITE_EXPECTED_BEFORE_ORACLE" in script[
        primary_mutation:primary_oracle
    ]
    assert "AUTOSPORT_TEST_REPLACE_EXPECTED_BEFORE_ORACLE" in script[
        primary_mutation:primary_oracle
    ]


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows PyInstaller regression",
)
def test_expected_snapshot_pre_oracle_same_object_write_fails_replica_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_TEST_WRITE_EXPECTED_BEFORE_ORACLE", "1")

    completed, _artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
        tmp_path
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert "expected mutation result failed independent replica authentication" in combined
    assert not bound.exists()
    assert not digest.exists()


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows PyInstaller regression",
)
def test_expected_snapshot_pre_oracle_replacement_fails_replica_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_TEST_REPLACE_EXPECTED_BEFORE_ORACLE", "1")

    completed, _artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
        tmp_path
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert "expected mutation result failed independent replica authentication" in combined
    assert not bound.exists()
    assert not digest.exists()
