from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from autosport import release_package as canonical_release
from autosport import stage_neutral_release as stage_release


SOURCE_SHA = "a" * 40


def _canonical_json(payload: dict[str, object]) -> bytes:
    return canonical_release._canonical_json_bytes(payload)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _legacy_members() -> dict[str, bytes]:
    exe = b"MZ-stage-neutral-test"
    build_info = {
        "autosport_exe_sha256": _sha256(exe),
        "human_tested": False,
        "nvda_verified": False,
        "product": "Autosport",
        "real_money_execution": False,
        "source_sha": SOURCE_SHA,
        "version": stage_release.LEGACY_BUILD_VERSION,
    }
    relative: dict[str, bytes] = {
        "Autosport.exe": exe,
        "BUILD_INFO.json": _canonical_json(build_info),
        "WINDOWS_START_HERE.txt": (
            "АВТОСПОРТ — V1 WINDOWS PAPER / MARKET LAB\n"
            "Перевірте <Autosport-V1-windows-x64.zip>.\n"
            "V1_READY=false\n"
        ).encode("utf-8"),
    }
    manifest = {
        "schema_version": 1,
        "files": {
            name: _sha256(payload)
            for name, payload in relative.items()
        },
    }
    relative["PACKAGE_MANIFEST.json"] = _canonical_json(manifest)
    sums = {
        name: _sha256(payload)
        for name, payload in relative.items()
    }
    relative["SHA256SUMS.txt"] = "".join(
        f"{sums[name]}  {name}\n" for name in sorted(sums)
    ).encode("utf-8")
    return relative


def _write_legacy_package(path: Path, members: dict[str, bytes] | None = None) -> None:
    relative = _legacy_members() if members is None else members
    canonical_release._write_canonical_zip(
        path,
        {
            f"{stage_release.LEGACY_PREFIX}{name}": payload
            for name, payload in relative.items()
        },
    )


def _stub_deep_verifier(monkeypatch: pytest.MonkeyPatch, *, status: str = "PASS") -> None:
    def verify(package: Path, *, expected_source_sha: str) -> dict[str, object]:
        assert package.name == stage_release.LEGACY_ARCHIVE_NAME
        assert expected_source_sha == SOURCE_SHA
        return {
            "status": status,
            "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        }

    monkeypatch.setattr(canonical_release, "verify_windows_package", verify)


def test_repackage_stage_neutral_release_rewrites_identity_and_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_deep_verifier(monkeypatch)
    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    output = tmp_path / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    _write_legacy_package(legacy)
    legacy_bytes = legacy.read_bytes()

    evidence = stage_release.repackage_stage_neutral_windows_release(
        legacy,
        output,
        expected_source_sha=SOURCE_SHA,
    )

    assert legacy.read_bytes() == legacy_bytes
    assert evidence == {
        "status": "PASS",
        "source_sha": SOURCE_SHA,
        "legacy_archive_name": stage_release.LEGACY_ARCHIVE_NAME,
        "archive_name": stage_release.STAGE_NEUTRAL_ARCHIVE_NAME,
        "package_prefix": stage_release.STAGE_NEUTRAL_PREFIX,
        "build_version": stage_release.STAGE_NEUTRAL_BUILD_VERSION,
        "package_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "file_count": 5,
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
        "whole_product_complete": False,
        "legacy_package_sha256": hashlib.sha256(legacy_bytes).hexdigest(),
    }

    with zipfile.ZipFile(output, "r") as archive:
        names = archive.namelist()
        assert names
        assert all(name.startswith(stage_release.STAGE_NEUTRAL_PREFIX) for name in names)
        assert not any(stage_release.LEGACY_PREFIX in name for name in names)
        build_payload = archive.read(
            f"{stage_release.STAGE_NEUTRAL_PREFIX}BUILD_INFO.json"
        )
        build_info = json.loads(build_payload)
        assert build_info["version"] == stage_release.STAGE_NEUTRAL_BUILD_VERSION
        assert build_info["whole_product_complete"] is False
        assert build_info["real_money_execution"] is False
        assert build_info["human_tested"] is False
        assert build_info["nvda_verified"] is False
        assert build_payload == canonical_release._canonical_json_bytes(build_info)

        manifest_payload = archive.read(
            f"{stage_release.STAGE_NEUTRAL_PREFIX}PACKAGE_MANIFEST.json"
        )
        manifest = json.loads(manifest_payload)
        assert manifest_payload == canonical_release._canonical_json_bytes(manifest)

        guide_payload = archive.read(
            f"{stage_release.STAGE_NEUTRAL_PREFIX}WINDOWS_START_HERE.txt"
        )
        guide = guide_payload.decode("utf-8")
        assert "V1 WINDOWS" not in guide
        assert "Autosport-V1-windows-x64.zip" not in guide
        assert "V1_READY" not in guide

        relative = {
            name[len(stage_release.STAGE_NEUTRAL_PREFIX) :]: archive.read(name)
            for name in names
        }
    stage_release._validate_manifest(relative, label="test stage-neutral package")
    stage_release._validate_sums(relative, label="test stage-neutral package")


def test_repackage_stage_neutral_release_is_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_deep_verifier(monkeypatch)
    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    _write_legacy_package(legacy)
    first = tmp_path / "first" / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    second = tmp_path / "second" / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME

    first_evidence = stage_release.repackage_stage_neutral_windows_release(
        legacy,
        first,
        expected_source_sha=SOURCE_SHA,
    )
    second_evidence = stage_release.repackage_stage_neutral_windows_release(
        legacy,
        second,
        expected_source_sha=SOURCE_SHA,
    )

    assert first.read_bytes() == second.read_bytes()
    assert first_evidence["package_sha256"] == second_evidence["package_sha256"]


def test_repackage_requires_existing_deep_verifier_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_deep_verifier(monkeypatch, status="FAIL")
    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    output = tmp_path / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    _write_legacy_package(legacy)

    with pytest.raises(ValueError, match="legacy release verifier did not return PASS"):
        stage_release.repackage_stage_neutral_windows_release(
            legacy,
            output,
            expected_source_sha=SOURCE_SHA,
        )
    assert not output.exists()


def test_repackage_rejects_verified_snapshot_digest_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    output = tmp_path / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    _write_legacy_package(legacy)

    def verify(package: Path, *, expected_source_sha: str) -> dict[str, object]:
        assert expected_source_sha == SOURCE_SHA
        assert package == legacy
        return {
            "status": "PASS",
            "package_sha256": "0" * 64,
        }

    monkeypatch.setattr(canonical_release, "verify_windows_package", verify)

    with pytest.raises(ValueError, match="changed after canonical verification"):
        stage_release.repackage_stage_neutral_windows_release(
            legacy,
            output,
            expected_source_sha=SOURCE_SHA,
        )
    assert not output.exists()


def test_repackage_rejects_nonlegacy_member_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_deep_verifier(monkeypatch)
    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    output = tmp_path / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    canonical_release._write_canonical_zip(
        legacy,
        {
            "Autosport-V1/BUILD_INFO.json": b"{}\n",
            "Other/file.txt": b"collision",
        },
    )

    with pytest.raises(ValueError, match="outside Autosport-V1/"):
        stage_release.repackage_stage_neutral_windows_release(
            legacy,
            output,
            expected_source_sha=SOURCE_SHA,
        )


def test_repackage_rejects_unexpected_legacy_build_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_deep_verifier(monkeypatch)
    members = _legacy_members()
    build_info = json.loads(members["BUILD_INFO.json"])
    build_info["version"] = "0.1.0-v2-prehuman"
    members["BUILD_INFO.json"] = _canonical_json(build_info)
    manifest = json.loads(members["PACKAGE_MANIFEST.json"])
    manifest["files"]["BUILD_INFO.json"] = _sha256(members["BUILD_INFO.json"])
    members["PACKAGE_MANIFEST.json"] = _canonical_json(manifest)
    sums = {
        name: _sha256(payload)
        for name, payload in members.items()
        if name != "SHA256SUMS.txt"
    }
    members["SHA256SUMS.txt"] = "".join(
        f"{sums[name]}  {name}\n" for name in sorted(sums)
    ).encode("utf-8")

    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    output = tmp_path / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    _write_legacy_package(legacy, members)

    with pytest.raises(ValueError, match="exact transitional version"):
        stage_release.repackage_stage_neutral_windows_release(
            legacy,
            output,
            expected_source_sha=SOURCE_SHA,
        )


def test_cli_writes_canonical_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_deep_verifier(monkeypatch)
    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    output = tmp_path / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    evidence_path = tmp_path / "stage-neutral-release-evidence.json"
    _write_legacy_package(legacy)

    assert (
        stage_release.main(
            [
                "--input",
                str(legacy),
                "--output",
                str(output),
                "--source-sha",
                SOURCE_SHA,
                "--evidence",
                str(evidence_path),
            ]
        )
        == 0
    )
    stdout_payload = json.loads(capsys.readouterr().out)
    evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert stdout_payload == evidence_payload
    assert evidence_path.read_bytes() == canonical_release._canonical_json_bytes(
        evidence_payload
    )
    assert evidence_payload["status"] == "PASS"
    assert evidence_payload["whole_product_complete"] is False
