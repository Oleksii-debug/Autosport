from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/derive_previous_release_fixture.py"
SOURCE_SHA = "a" * 40


def _module():
    spec = importlib.util.spec_from_file_location("derive_previous_release_fixture", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_derivation_reseals_prior_version_and_preserves_truth(monkeypatch, tmp_path):
    module = _module()
    current = tmp_path / "current.zip"
    current.write_bytes(b"exact-current-package")
    current_sha = hashlib.sha256(current.read_bytes()).hexdigest()
    output = tmp_path / "previous.zip"
    build = {
        "product": "Autosport",
        "source_sha": SOURCE_SHA,
        "version": "0.1.0",
        "update_version": "0.1.0",
        "update_schema": 1,
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
    }
    members = {
        "Autosport.exe": b"MZ-fixture",
        "BUILD_INFO.json": module.release_package._canonical_json_bytes(build),
        "PACKAGE_MANIFEST.json": b"{}",
        "SHA256SUMS.txt": b"placeholder",
    }

    def verify(path, **_kwargs):
        return {
            "status": "PASS",
            "package_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        }

    monkeypatch.setattr(module.release_package, "verify_windows_package", verify)
    monkeypatch.setattr(
        module.data_tool_package, "verify_portable_data_tool", lambda *_a, **_k: {"status": "PASS"}
    )
    monkeypatch.setattr(
        module.data_tool_package, "_read_members", lambda _stream: dict(members)
    )

    evidence = module.derive_previous_release_fixture(
        current,
        output,
        expected_source_sha=SOURCE_SHA,
        expected_package_sha256=current_sha,
    )

    assert evidence["status"] == "PASS"
    assert evidence["evidence_class"] == "FIXTURE_ONLY_PREVIOUS_VERSION_ENVELOPE"
    assert evidence["historical_release"] is False
    assert evidence["update_version"] == "0.0.0"
    assert evidence["update_schema"] == 1
    assert evidence["package_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    with zipfile.ZipFile(output) as archive:
        transformed = json.loads(archive.read("Autosport-V1/BUILD_INFO.json"))
    assert transformed["version"] == transformed["update_version"] == "0.0.0"
    assert transformed["update_schema"] == 1
    assert transformed["real_money_execution"] is False
    assert transformed["human_tested"] is False
    assert transformed["nvda_verified"] is False


def test_fixture_derivation_rejects_input_output_alias_before_mutation(tmp_path):
    module = _module()
    archive = tmp_path / "same.zip"
    archive.write_bytes(b"preserve-me")

    try:
        module.derive_previous_release_fixture(
            archive,
            archive,
            expected_source_sha=SOURCE_SHA,
            expected_package_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        )
    except ValueError as exc:
        assert "cannot overwrite" in str(exc)
    else:
        raise AssertionError("input/output alias unexpectedly accepted")
    assert archive.read_bytes() == b"preserve-me"
