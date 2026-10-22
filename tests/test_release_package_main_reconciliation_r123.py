"""Plan 6/6Б: preserve current-main release security during update-metadata integration.

All archives and identities are synthetic. No installation, account or real-money action.
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from autosport import release_package as release
from test_windows_update_release_package_integration import SOURCE_SHA, _build, _fixture


def _repacked(tmp_path: Path, *, update_version: str | None = None, add: str | None = None) -> Path:
    source = _build(tmp_path, **({"update_version": update_version, "update_schema": 2} if update_version else {}))
    with zipfile.ZipFile(source) as archive:
        files = {item.filename.removeprefix("Autosport-V1/"): archive.read(item) for item in archive.infolist()}
    if add:
        files[add] = b"synthetic fixture only"
    else:
        info = json.loads(files["BUILD_INFO.json"])
        info["version"] = "9.9.9"  # invalid for an unsigned legacy package
        files["BUILD_INFO.json"] = release._canonical_json_bytes(info)
    files.pop("PACKAGE_MANIFEST.json")
    files.pop("SHA256SUMS.txt")
    manifest = {"schema_version": 1, "files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
    files["PACKAGE_MANIFEST.json"] = release._canonical_json_bytes(manifest)
    files["SHA256SUMS.txt"] = ("\n".join(f"{hashlib.sha256(files[k]).hexdigest()}  {k}" for k in sorted(files)) + "\n").encode()
    repacked = tmp_path / "resealed.zip"
    release._write_canonical_zip(repacked, {f"Autosport-V1/{k}": v for k, v in files.items()})
    return repacked


def test_legacy_version_forgery_rejected_even_with_fresh_hashes(tmp_path):
    with pytest.raises(ValueError, match="prehuman version"):
        release.verify_windows_package(_repacked(tmp_path), expected_source_sha=SOURCE_SHA)


@pytest.mark.parametrize("relative", [
    ".env", ".env.production", "token.json", "credentials.json",
    "nested/.pytest_cache/state", "nested/__pycache__/module.pyc",
    "nested/.ssh/id_ed25519", "nested/Local State", "nested/client_secret_test.json",
    "nested/data.session", "nested/private.pem", "nested/file.key",
])
def test_resealed_private_or_test_member_fails_before_manifest_trust(tmp_path, relative):
    with pytest.raises(ValueError, match="forbidden private/test state"):
        release.verify_windows_package(_repacked(tmp_path, add=relative), expected_source_sha=SOURCE_SHA)


@pytest.mark.parametrize("relative", [".env.example", "docs/certificate.pem", "examples/demo.jsonl"])
def test_legitimate_release_names_are_not_blocked(relative):
    path, _ = release._validate_windows_member("Autosport-V1/" + relative)
    assert path == relative


def test_build_does_not_remove_unrelated_preexisting_release_directory(tmp_path):
    existing = tmp_path / "Autosport-V1"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("preserve user state", encoding="utf-8")
    package = _build(tmp_path, update_version="1.2.3", update_schema=2)
    assert package.is_file()
    assert sentinel.read_text(encoding="utf-8") == "preserve user state"
    assert release.verify_windows_package(package, expected_source_sha=SOURCE_SHA)["status"] == "PASS"


def test_failed_release_build_preserves_prior_zip_and_existing_directory(tmp_path):
    existing = tmp_path / "Autosport-V1"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("preserve user state", encoding="utf-8")
    zip_path = tmp_path / "candidate.zip"
    zip_path.write_bytes(b"old package stays until publication")
    f = _fixture(tmp_path)
    f["exe"].unlink()  # source failure during private staging
    # Canonical preflight now maps missing EXE input to a deterministic
    # fail-closed ValueError before staging, preserving the existing ZIP.
    with pytest.raises(ValueError, match="release executable must be an existing regular file"):
        release.build_windows_package(
            f["exe"], f["start"], f["examples"], f["diagnostic"],
            f["accessibility"], f["keyboard"], f["restart"],
            zip_path, SOURCE_SHA, update_version="1.2.3", update_schema=2,
        )
    assert zip_path.read_bytes() == b"old package stays until publication"
    assert sentinel.read_text(encoding="utf-8") == "preserve user state"
    assert not list(tmp_path.glob(".autosport-release-stage-*"))
