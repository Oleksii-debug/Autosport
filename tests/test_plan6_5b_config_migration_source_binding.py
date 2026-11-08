"""Plan 6 §5Б: immutable package declarations include actual config/schema authorities.

These declarations bind source bytes to the exact commit; they do not constitute
an installed-package SBOM, license clearance, or a successful Windows release.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport.release_package import (
    _SOURCE_MANIFEST_INPUTS,
    _validate_source_manifest,
)
from scripts import package_windows


SOURCE_SHA = "a" * 40
CONFIG_AND_MIGRATION_SOURCES = frozenset({
    "src/autosport/windows_entry.py",
    "src/autosport/windows_layout.py",
    "src/autosport/paths.py",
    "src/autosport/storage.py",
    "src/autosport/workspace_lock.py",
    "src/autosport/monotonic_workspace_authority.py",
    "src/autosport/market_store_migration.py",
})
PYPROJECT = (
    b'[project]\n'
    b'dependencies = ["tk-uia==0.8.0", "APScheduler==3.11.3"]\n'
    b'[project.optional-dependencies]\n'
    b'build = ["pyinstaller==6.22.3"]\n'
)


def _git_blob(repo_root: Path, command: str, revision_path: str) -> bytes:
    assert command == "show"
    expected_prefix = SOURCE_SHA + ":"
    assert revision_path.startswith(expected_prefix)
    path = revision_path[len(expected_prefix):]
    assert path in package_windows._RELEASE_SOURCE_INPUTS
    if path == "pyproject.toml":
        return PYPROJECT
    return ("immutable git-source " + path).encode("utf-8")


def _manifest():
    with patch.object(package_windows, "_exact_git_bytes", side_effect=_git_blob):
        return package_windows._source_declaration_manifest(Path("."), SOURCE_SHA)


def test_package_and_verifier_require_same_complete_source_set():
    assert set(package_windows._RELEASE_SOURCE_INPUTS) == set(_SOURCE_MANIFEST_INPUTS)
    assert len(package_windows._RELEASE_SOURCE_INPUTS) == len(set(package_windows._RELEASE_SOURCE_INPUTS))
    assert CONFIG_AND_MIGRATION_SOURCES <= set(_SOURCE_MANIFEST_INPUTS)


def test_exact_git_config_migration_blobs_are_in_canonical_manifest():
    manifest = _manifest()
    _validate_source_manifest(manifest, SOURCE_SHA)
    source_hashes = manifest["source_files_sha256"]
    for path in CONFIG_AND_MIGRATION_SOURCES:
        assert source_hashes[path] == hashlib.sha256(
            _git_blob(Path("."), "show", SOURCE_SHA + ":" + path)
        ).hexdigest()


@pytest.mark.parametrize("missing", sorted(CONFIG_AND_MIGRATION_SOURCES))
def test_verifier_rejects_dropped_configuration_or_migration_provenance(missing):
    manifest = deepcopy(_manifest())
    manifest["source_files_sha256"].pop(missing)
    with pytest.raises(ValueError, match="source input set"):
        _validate_source_manifest(manifest, SOURCE_SHA)


@pytest.mark.parametrize("bad_digest", ["", "z" * 64, "A" * 64, 0, None])
def test_verifier_rejects_bad_source_digest(bad_digest):
    manifest = deepcopy(_manifest())
    manifest["source_files_sha256"]["src/autosport/storage.py"] = bad_digest
    with pytest.raises(ValueError, match="source digest"):
        _validate_source_manifest(manifest, SOURCE_SHA)


def test_exact_git_missing_config_authority_fails_before_manifest_creation():
    def missing_blob(root: Path, command: str, revision_path: str):
        if revision_path.endswith(":src/autosport/storage.py"):
            raise ValueError("exact Git source missing")
        return _git_blob(root, command, revision_path)

    with patch.object(package_windows, "_exact_git_bytes", side_effect=missing_blob):
        with pytest.raises(ValueError, match="exact Git source missing"):
            package_windows._source_declaration_manifest(Path("."), SOURCE_SHA)
