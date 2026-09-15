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
    "_autosport_pyinstaller_producer_handoff_tests_pkg_authority",
    _PRODUCER_TEST_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
_PRODUCER_TESTS = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _PRODUCER_TESTS
_SPEC.loader.exec_module(_PRODUCER_TESTS)

_REAL_WINDOWS_PYINSTALLER = (
    os.name == "nt" and importlib.util.find_spec("PyInstaller") is not None
)


def test_pkg_authority_is_retained_from_carchive_writer_through_both_append_consumers() -> None:
    script = _GUARDED_PYINSTALLER.read_text(encoding="utf-8")

    writer = script.index("def guarded_carchive_writer(")
    authoritative_open = script.index("_open_retained_package_writer(pkg_path)", writer)
    publish = script.index("package_authorities[pkg_key] = {", authoritative_open)
    append = script.index("def guarded_append_data(", publish)
    validate_before = script.index(
        '_validate_package_authority(authority, phase="before append consumption")',
        append,
    )
    retained_read = script.index("stream.seek(0)", validate_before)
    copy = script.index("shutil.copyfileobj(stream, outf, length=64 * 1024)", retained_read)
    validate_after = script.index(
        '_validate_package_authority(authority, phase="after append consumption")',
        copy,
    )
    release = script.index('if authority["uses"] == 2:', validate_after)

    assert writer < authoritative_open < publish < append
    assert append < validate_before < retained_read < copy < validate_after < release
    assert "building_api.CArchiveWriter = guarded_carchive_writer" in script
    assert "building_api.EXE._append_data_to_exe = guarded_append_data" in script
    assert "without producer-bound PKG authority" in script
    assert "_FILE_SHARE_READ" in script


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows PyInstaller regression",
)
def test_pkg_same_object_write_is_blocked_in_producer_to_append_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_TEST_WRITE_PKG_BEFORE_APPEND", "1")

    completed, _artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
        tmp_path
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert "PKG producer fence blocked hostile same-object write before append" in combined
    assert not bound.exists()
    assert not digest.exists()


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows PyInstaller regression",
)
def test_pkg_same_path_replacement_is_blocked_in_producer_to_append_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_TEST_REPLACE_PKG_BEFORE_APPEND", "1")

    completed, _artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
        tmp_path
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert "PKG producer fence blocked hostile same-path replacement before append" in combined
    assert not bound.exists()
    assert not digest.exists()
