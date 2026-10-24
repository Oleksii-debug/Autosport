"""Plan 6 §5Б: exercise real release_sbom license capture against fake RECORD.

These are source-level regression tests, not Windows EXE or legal clearance.
"""
from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace

import pytest

from autosport import release_sbom

SOURCE = "a" * 40


class Record:
    def __init__(self, path, payload):
        self.path = path
        self.size = len(payload)
        self.hash = SimpleNamespace(
            mode="sha256",
            value=base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
            .rstrip(b"=").decode("ascii"),
        )

    def __str__(self):
        return self.path


class FakeDistribution:
    def __init__(self, name, version, records, root):
        self.metadata = {"Name": name}
        self.version = version
        self.files = []
        self.root = root
        for path, payload in records:
            entry = Record(path, payload)
            self.files.append(entry)
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(payload)

    def locate_file(self, record):
        return self.root / str(record)


def capture(monkeypatch, distributions):
    monkeypatch.setattr(
        release_sbom.metadata, "distribution", lambda name: distributions[name]
    )
    graph = {
        "source_sha": SOURCE,
        "components": [
            {"name": name, "version": dist.version}
            for name, dist in sorted(distributions.items())
        ],
    }
    return release_sbom.capture_dependency_license_evidence(graph)


def test_packaging_three_license_files_are_captured(tmp_path, monkeypatch):
    prefix = "packaging-25.0.dist-info/licenses/"
    dist = FakeDistribution("packaging", "25.0", [
        (prefix + "LICENSE", b"MIT license\n"),
        (prefix + "LICENSE.APACHE", b"Apache license\n"),
        (prefix + "LICENSE.BSD", b"BSD license\n"),
    ], tmp_path)
    manifest, members = capture(monkeypatch, {"packaging": dist})
    assert len(manifest["distributions"][0]["files"]) == 3
    assert set(members) == {f"licenses/packaging/{n:03d}.txt" for n in range(3)}
    assert manifest["distributions"][0]["license_review"] == "TEXT_PRESENT_REVIEW_REQUIRED"
    assert b"Apache license\n" in members.values()
    assert b"BSD license\n" in members.values()


def test_setuptools_vendored_license_is_captured(tmp_path, monkeypatch):
    dist = FakeDistribution("setuptools", "84.2.1", [
        ("setuptools-84.2.1.dist-info/licenses/LICENSE", b"setuptools\n"),
        ("setuptools/_vendor/packaging-26.3.dist-info/licenses/LICENSE", b"vendor\n"),
        ("setuptools/_vendor/packaging-26.3.dist-info/licenses/LICENSE.BSD", b"BSD\n"),
    ], tmp_path)
    manifest, members = capture(monkeypatch, {"setuptools": dist})
    assert len(manifest["distributions"][0]["files"]) == 3
    assert len(members) == 3


@pytest.mark.parametrize("mutation", [
    "same_size", "truncated", "missing_size", "missing_hash", "wrong_algorithm",
])
def test_record_tampering_fails_closed(tmp_path, monkeypatch, mutation):
    dist = FakeDistribution("packaging", "25.0", [
        ("packaging-25.0.dist-info/licenses/LICENSE", b"Original\n"),
    ], tmp_path)
    entry = dist.files[0]
    if mutation == "same_size":
        dist.locate_file(entry).write_bytes(b"Tampered\n")
    elif mutation == "truncated":
        dist.locate_file(entry).write_bytes(b"Short\n")
    elif mutation == "missing_size":
        entry.size = None
    elif mutation == "missing_hash":
        entry.hash = None
    else:
        entry.hash.mode = "sha512"
    with pytest.raises(ValueError, match="RECORD digest/size mismatch"):
        capture(monkeypatch, {"packaging": dist})


@pytest.mark.parametrize("path", [
    "other-25.0.dist-info/licenses/LICENSE",
    "packaging-26.3.dist-info/licenses/LICENSE",
    "setuptools/_vendor/packaging-26.3.dist-info/licenses/LICENSE",
])
def test_wrong_owner_is_rejected(tmp_path, monkeypatch, path):
    dist = FakeDistribution("packaging", "25.0", [(path, b"license\n")], tmp_path)
    with pytest.raises(ValueError, match="origin"):
        capture(monkeypatch, {"packaging": dist})


@pytest.mark.parametrize("path", [
    "packaging-25.0.dist-info/licenses/LICENSE.GPL",
    "packaging-25.0.dist-info/licenses/../LICENSE",
])
def test_unrecognized_or_unsafe_paths_are_not_claimed(tmp_path, monkeypatch, path):
    dist = FakeDistribution("packaging", "25.0", [(path, b"license\n")], tmp_path)
    manifest, members = capture(monkeypatch, {"packaging": dist})
    assert manifest["distributions"][0]["files"] == []
    assert members == {}


def test_repeat_capture_is_deterministic(tmp_path, monkeypatch):
    dist = FakeDistribution("packaging", "25.0", [
        ("packaging-25.0.dist-info/licenses/LICENSE", b"License\n"),
    ], tmp_path)
    first = capture(monkeypatch, {"packaging": dist})
    second = capture(monkeypatch, {"packaging": dist})
    assert first == second


def test_owner_mismatch_diagnostic_is_bounded_to_relative_record(tmp_path, monkeypatch):
    """Expose the failing wheel RECORD origin without leaking host paths."""
    origin = "other-25.0.dist-info/licenses/LICENSE"
    dist = FakeDistribution("packaging", "25.0", [(origin, b"license\n")], tmp_path)
    with pytest.raises(ValueError, match="RECORD origin mismatches") as error:
        capture(monkeypatch, {"packaging": dist})
    detail = str(error.value)
    assert "component='packaging'" in detail
    assert "version='25.0'" in detail
    assert f"record='{origin}'" in detail
    assert str(tmp_path) not in detail
