"""Plan 6 §5Б: source-bound LICENSE/NOTICE byte copies, not legal clearance.

A license file coming from a build host is useful distribution evidence but is
not proof that the wheel was embedded in PyInstaller's sealed executable.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import hashlib
import os

import pytest

import autosport.release_sbom as subject


SHA = "a" * 40


def _graph():
    return {
        "schema_version": 1,
        "source_sha": SHA,
        "evidence_class": "BUILD_HOST_REQUIREMENT_GRAPH_ONLY",
        "components": [{
            "name": "example-dist", "version": "1.2.3",
            "direct_roles": ["runtime"], "requires": [],
            "license_expression": None, "license_evidence": "NOT_PROVIDED",
        }],
    }


def _distribution(root: Path, *, with_license: bool = True):
    path = PurePosixPath("example_dist-1.2.3.dist-info/licenses/LICENSE.txt")
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_bytes(b"Example copyright and license terms\n")
    return SimpleNamespace(
        metadata={"Name": "example-dist"},
        version="1.2.3",
        files=[path] if with_license else [],
        locate_file=lambda value: root.joinpath(*value.parts),
    )


def test_real_license_bytes_are_bound_to_distribution_and_source(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(subject.metadata, "distribution",
                        lambda name: _distribution(tmp_path))
    manifest, members = subject.capture_dependency_license_evidence(_graph())
    assert manifest["evidence_class"] == "BUILD_HOST_LICENSE_TEXT_ONLY_NOT_CLEARANCE"
    assert manifest["distributions"][0]["license_review"] == "TEXT_PRESENT_REVIEW_REQUIRED"
    assert list(members) == ["licenses/example-dist/000.txt"]
    assert manifest["distributions"][0]["files"][0]["sha256"] == hashlib.sha256(
        members["licenses/example-dist/000.txt"]
    ).hexdigest()
    subject.validate_dependency_license_evidence(manifest, _graph(), members)


def test_absent_license_text_never_claims_clearance(tmp_path: Path, monkeypatch):
    dist = _distribution(tmp_path, with_license=False)
    monkeypatch.setattr(subject.metadata, "distribution", lambda name: dist)
    manifest, members = subject.capture_dependency_license_evidence(_graph())
    assert members == {}
    assert manifest["distributions"][0]["files"] == []
    assert manifest["distributions"][0]["license_review"] == "TEXT_ABSENT_REVIEW_REQUIRED"
    forged = deepcopy(manifest)
    forged["distributions"][0]["license_review"] = "LICENSE_CLEARED"
    with pytest.raises(ValueError, match="cannot claim clearance"):
        subject.validate_dependency_license_evidence(forged, _graph(), members)


@pytest.mark.parametrize("mutation", [
    "wrong_sha", "source_sha", "unknown_member", "changed_bytes", "extra_file",
    "fake_approval", "unsafe_origin", "wrong_version",
])
def test_resealed_or_mismatched_license_evidence_is_rejected(
    tmp_path: Path, monkeypatch, mutation: str,
):
    monkeypatch.setattr(subject.metadata, "distribution",
                        lambda name: _distribution(tmp_path))
    manifest, members = subject.capture_dependency_license_evidence(_graph())
    manifest = deepcopy(manifest)
    members = dict(members)
    record = manifest["distributions"][0]["files"][0]
    if mutation == "wrong_sha":
        record["sha256"] = "0" * 64
    elif mutation == "source_sha":
        manifest["source_sha"] = "0" * 40
    elif mutation == "unknown_member":
        record["path"] = "licenses/another-dist/000.txt"
    elif mutation == "changed_bytes":
        members["licenses/example-dist/000.txt"] += b"tampered"
    elif mutation == "extra_file":
        members["licenses/example-dist/001.txt"] = b"unclaimed"
    elif mutation == "fake_approval":
        manifest["evidence_class"] = "LICENSE_CLEARED"
    elif mutation == "unsafe_origin":
        record["record_path"] = "../secret.txt"
    elif mutation == "wrong_version":
        manifest["distributions"][0]["version"] = "9.9.9"
    with pytest.raises(ValueError):
        subject.validate_dependency_license_evidence(manifest, _graph(), members)


def test_bundled_evidence_is_on_real_windows_package_path():
    root = Path(__file__).resolve().parents[1]
    package = (root / "src/autosport/release_package.py").read_text(encoding="utf-8")
    official = (root / "scripts/package_windows.py").read_text(encoding="utf-8")
    assert "DEPENDENCY_LICENSE_EVIDENCE.json" in package
    assert "require_dependency_license_evidence=True" in official
    assert "dependency_license_members=dependency_license_members" in official


def test_swapped_license_inode_after_path_preflight_is_rejected(
    tmp_path: Path, monkeypatch,
):
    """The captured bytes must come from the inode whose lstat was checked."""
    dist = _distribution(tmp_path / "wheel")
    path = tmp_path / "wheel/example_dist-1.2.3.dist-info/licenses/LICENSE.txt"
    replacement = tmp_path / "other-license.txt"
    replacement.write_bytes(b"Replacement text with a different inode and ownership\n")
    original_lstat = Path.lstat
    replaced = False

    def replace_after_lstat(self, *args, **kwargs):
        nonlocal replaced
        observation = original_lstat(self, *args, **kwargs)
        if self == path and not replaced:
            replaced = True
            os.replace(replacement, path)
        return observation

    monkeypatch.setattr(subject.metadata, "distribution", lambda name: dist)
    monkeypatch.setattr(Path, "lstat", replace_after_lstat)
    with pytest.raises(ValueError, match="changed before read"):
        subject.capture_dependency_license_evidence(_graph())
    assert replaced


def test_license_capture_rejects_oversized_sources_without_reading(
    tmp_path: Path, monkeypatch,
):
    dist = _distribution(tmp_path)
    path = tmp_path / "example_dist-1.2.3.dist-info/licenses/LICENSE.txt"
    path.write_bytes(b"A" * 262145)
    monkeypatch.setattr(subject.metadata, "distribution", lambda name: dist)
    with pytest.raises(ValueError, match="bounded regular file"):
        subject.capture_dependency_license_evidence(_graph())


@pytest.mark.parametrize("foreign", [
    "another_dist-1.2.3.dist-info/licenses/LICENSE.txt",
    "example_dist-9.9.9.dist-info/licenses/LICENSE.txt",
    "nested/example_dist-1.2.3.dist-info/licenses/LICENSE.txt",
])
def test_foreign_license_record_cannot_be_credited_to_distribution(
    tmp_path: Path, monkeypatch, foreign: str,
):
    path = PurePosixPath(foreign)
    (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / path).write_bytes(b"Foreign distribution copyright notice\n")
    dist = _distribution(tmp_path, with_license=False)
    dist.files = [path]
    monkeypatch.setattr(subject.metadata, "distribution", lambda _: dist)
    with pytest.raises(ValueError, match="RECORD origin mismatches"):
        subject.capture_dependency_license_evidence(_graph())


@pytest.mark.parametrize("origin", [
    "another_dist-1.2.3.dist-info/licenses/LICENSE.txt",
    "example_dist-9.9.9.dist-info/licenses/LICENSE.txt",
    "example_dist-1.2.3.dist-info/licenses/secrets.txt",
    "nested/example_dist-1.2.3.dist-info/licenses/LICENSE.txt",
    "example_dist-1.2.3.dist-info/licenses/NOTICE.txt/../LICENSE.txt",
])
def test_resealed_license_evidence_rejects_foreign_or_non_license_origin(
    tmp_path: Path, monkeypatch, origin: str,
):
    monkeypatch.setattr(subject.metadata, "distribution",
                        lambda _: _distribution(tmp_path))
    manifest, members = subject.capture_dependency_license_evidence(_graph())
    forged = deepcopy(manifest)
    forged["distributions"][0]["files"][0]["record_path"] = origin
    with pytest.raises(ValueError, match="origin path is unsafe"):
        subject.validate_dependency_license_evidence(forged, _graph(), members)
