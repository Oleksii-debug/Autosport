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
    "_autosport_pyinstaller_producer_handoff_tests_expected_authority",
    _PRODUCER_TEST_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
_PRODUCER_TESTS = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _PRODUCER_TESTS
_SPEC.loader.exec_module(_PRODUCER_TESTS)

_REAL_WINDOWS_PYINSTALLER = (
    os.name == "nt" and importlib.util.find_spec("PyInstaller") is not None
)


def test_expected_snapshot_uses_continuous_authority_instead_of_replica_replay() -> None:
    script = _GUARDED_PYINSTALLER.read_text(encoding="utf-8")

    materialize = script.index("def _make_expected_snapshot(")
    transition = script.index("def _trusted_byte_transition(", materialize)
    resource_begin = script.index("def guarded_expected_begin_update_resource(", transition)
    native_begin = script.index("native_handle = original_begin_update_resource(", resource_begin)
    native_access_proof = script.index("_require_windows_access_denied(", native_begin)
    acl_fence = script.index("_set_expected_snapshot_write_fence(", native_access_proof)
    resource_end = script.index("def guarded_expected_end_update_resource(", acl_fence)
    native_end = script.index("result = original_end_update_resource(", resource_end)
    post_end_access_proof = script.index("post-EndUpdateResource ACL write exclusion", native_end)
    post_end_probe = script.index("AUTOSPORT_TEST_WRITE_EXPECTED_AFTER_RESOURCE_END", post_end_access_proof)
    post_commit_oracle = script.index("next_oracle, next_identity = _open_expected_snapshot_oracle(", post_end_probe)
    release_acl = script.index("_remove_expected_snapshot_write_fence(", post_commit_oracle)
    live_mutation = script.index("live_result = live_mutator()", release_acl)

    assert materialize < transition < resource_begin < native_begin < native_access_proof
    assert native_access_proof < acl_fence < resource_end < native_end
    assert native_end < post_end_access_proof < post_end_probe < post_commit_oracle
    assert post_commit_oracle < release_acl < live_mutation
    assert "reference_snapshot" not in script[transition:live_mutation]
    assert "reference_digest" not in script[transition:live_mutation]
    assert "_RetainedArtifactAppender(expected_stream)" in script[transition:live_mutation]
    assert "_RetainedArtifactWriter(expected_stream)" in script[transition:live_mutation]
    assert "resource_win32api.BeginUpdateResource = guarded_expected_begin_update_resource" in script[
        transition:live_mutation
    ]
    assert "resource_win32api.EndUpdateResource = guarded_expected_end_update_resource" in script[
        transition:live_mutation
    ]


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows PyInstaller regression",
)
def test_expected_snapshot_pre_publication_same_object_write_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_TEST_WRITE_EXPECTED_BEFORE_ORACLE", "1")

    completed, _artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
        tmp_path
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert "continuous expected authority blocked pre-publication same-object write" in combined
    assert not bound.exists()
    assert not digest.exists()


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows PyInstaller regression",
)
def test_expected_snapshot_pre_publication_replacement_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_TEST_REPLACE_EXPECTED_BEFORE_ORACLE", "1")

    completed, _artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
        tmp_path
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert "continuous expected authority blocked pre-publication replacement" in combined
    assert not bound.exists()
    assert not digest.exists()


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows PyInstaller regression",
)
def test_native_resource_update_blocks_same_object_write_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_TEST_WRITE_EXPECTED_DURING_RESOURCE_UPDATE", "1")

    completed, _artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
        tmp_path
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert "native resource update blocked hostile expected same-object write" in combined
    assert not bound.exists()
    assert not digest.exists()


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows PyInstaller regression",
)
def test_native_resource_update_blocks_replacement_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_TEST_REPLACE_EXPECTED_DURING_RESOURCE_UPDATE", "1")

    completed, _artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
        tmp_path
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert "native resource update blocked hostile expected replacement" in combined
    assert not bound.exists()
    assert not digest.exists()


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows PyInstaller regression",
)
def test_post_end_resource_acl_fence_blocks_same_object_write_before_oracle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_TEST_WRITE_EXPECTED_AFTER_RESOURCE_END", "1")

    completed, _artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
        tmp_path
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert (
        "post-EndUpdateResource ACL fence blocked hostile expected same-object write before oracle"
        in combined
    )
    assert not bound.exists()
    assert not digest.exists()


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows PyInstaller regression",
)
def test_post_end_resource_acl_fence_blocks_replacement_before_oracle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_TEST_REPLACE_EXPECTED_AFTER_RESOURCE_END", "1")

    completed, _artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
        tmp_path
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert (
        "post-EndUpdateResource ACL fence blocked hostile expected replacement before oracle"
        in combined
    )
    assert not bound.exists()
    assert not digest.exists()
