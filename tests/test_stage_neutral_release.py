from __future__ import annotations

import hashlib
import os
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



def _reseal_legacy_metadata(members: dict[str, bytes]) -> None:
    """Make adversarial fixtures internally checksum-consistent."""
    manifest = {
        "schema_version": 1,
        "files": {
            key: _sha256(value)
            for key, value in members.items()
            if key not in {"PACKAGE_MANIFEST.json", "SHA256SUMS.txt"}
        },
    }
    members["PACKAGE_MANIFEST.json"] = _canonical_json(manifest)
    members["SHA256SUMS.txt"] = "".join(
        f"{_sha256(members[key])}  {key}\n"
        for key in sorted(members)
        if key != "SHA256SUMS.txt"
    ).encode("utf-8")


@pytest.mark.parametrize("field", ["v1_ready", "whole_product_complete"])
@pytest.mark.parametrize("value", [True, "true", 1])
def test_repack_rejects_checksum_valid_positive_readiness_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    _stub_deep_verifier(monkeypatch)
    members = _legacy_members()
    info = json.loads(members["BUILD_INFO.json"])
    info[field] = value
    members["BUILD_INFO.json"] = _canonical_json(info)
    _reseal_legacy_metadata(members)
    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    output = tmp_path / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    _write_legacy_package(legacy, members)
    with pytest.raises(ValueError, match=f"{field}=false"):
        stage_release.repackage_stage_neutral_windows_release(
            legacy, output, expected_source_sha=SOURCE_SHA,
        )
    assert not output.exists()


@pytest.mark.parametrize(
    "additional",
    [
        "V1_READY=true",
        "v1_ready = TRUE",
        "WHOLE_PRODUCT_COMPLETE=true",
        "WHOLE_PRODUCT_COMPLETE=unknown",
        "WHOLE_PRODUCT_COMPLETE=falseX",
        "WHOLE_PRODUCT_COMPLETE=false\nWHOLE_PRODUCT_COMPLETE=true",
    ],
)
def test_guide_rejects_residual_ready_claims(additional: str) -> None:
    guide = (
        f"{stage_release.LEGACY_GUIDE_TITLE}\n"
        "V1_READY=false\n"
        f"{additional}\n"
    )
    with pytest.raises(ValueError, match="contradictory readiness"):
        stage_release._transform_windows_start_guide(guide.encode("utf-8"))


def test_guide_rejects_missing_readiness_claim() -> None:
    guide = f"{stage_release.LEGACY_GUIDE_TITLE}\nNo readiness claim\n"
    with pytest.raises(ValueError, match="missing or contradictory readiness"):
        stage_release._transform_windows_start_guide(guide.encode("utf-8"))


def test_legacy_optional_explicit_false_readiness_is_compatible() -> None:
    members = _legacy_members()
    info = json.loads(members["BUILD_INFO.json"])
    info.update(v1_ready=False, whole_product_complete=False)
    members["BUILD_INFO.json"] = _canonical_json(info)
    _reseal_legacy_metadata(members)
    converted = stage_release._transform_members(
        members, expected_source_sha=SOURCE_SHA,
    )
    evidence = json.loads(converted["BUILD_INFO.json"])
    assert evidence["v1_ready"] is False
    assert evidence["whole_product_complete"] is False


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

def test_evidence_publication_replaces_receipt_atomically_on_unicode_path(
    tmp_path: Path,
) -> None:
    receipt = tmp_path / "збірка з пробілами" / "stage-neutral-evidence.json"
    prior = {"status": "PASS", "whole_product_complete": False, "epoch": 1}
    current = {"status": "PASS", "whole_product_complete": False, "epoch": 2}
    stage_release._write_evidence(receipt, prior)
    assert receipt.read_bytes() == _canonical_json(prior)
    stage_release._write_evidence(receipt, current)
    assert receipt.read_bytes() == _canonical_json(current)
    assert list(receipt.parent.glob(f".{receipt.name}.*.tmp")) == []


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_interrupted_evidence_publication_preserves_prior_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    receipt = tmp_path / "verified-receipt.json"
    prior = {"status": "PASS", "whole_product_complete": False, "epoch": 1}
    stage_release._write_evidence(receipt, prior)
    original_bytes = receipt.read_bytes()

    def interrupted(*args: object, **kwargs: object) -> None:
        raise OSError(f"synthetic {failure} interruption")

    monkeypatch.setattr(stage_release.os, failure, interrupted)
    with pytest.raises(OSError, match="synthetic .* interruption"):
        stage_release._write_evidence(
            receipt,
            {"status": "PASS", "whole_product_complete": False, "epoch": 2},
        )
    assert receipt.read_bytes() == original_bytes
    assert list(receipt.parent.glob(f".{receipt.name}.*.tmp")) == []


def test_transform_windows_start_guide_rejects_unknown_product_identity() -> None:
    with pytest.raises(ValueError, match="unrecognized product identity"):
        stage_release._transform_windows_start_guide(
            "АВТОСПОРТ — UNKNOWN STAGE\n".encode("utf-8")
        )


def test_transform_windows_start_guide_accepts_stage_neutral_source() -> None:
    payload = (
        f"{stage_release.STAGE_NEUTRAL_GUIDE_TITLE}\n"
        f"ZIP: {stage_release.LEGACY_ARCHIVE_NAME}\n"
        f"{stage_release.LEGACY_READY_LABEL}\n"
    ).encode("utf-8")

    transformed = stage_release._transform_windows_start_guide(payload).decode(
        "utf-8"
    )

    assert transformed.startswith(f"{stage_release.STAGE_NEUTRAL_GUIDE_TITLE}\n")
    assert stage_release.LEGACY_ARCHIVE_NAME not in transformed
    assert stage_release.STAGE_NEUTRAL_ARCHIVE_NAME in transformed
    assert stage_release.LEGACY_READY_LABEL not in transformed
    assert stage_release.STAGE_NEUTRAL_READY_LABEL in transformed

def test_stage_neutral_publish_does_not_destroy_previous_zip_on_verifier_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_deep_verifier(monkeypatch)
    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    output = tmp_path / "Ужгород з пробілами" / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    _write_legacy_package(legacy)
    stage_release.repackage_stage_neutral_windows_release(
        legacy, output, expected_source_sha=SOURCE_SHA
    )
    previous_verified_zip = output.read_bytes()
    original_writer = canonical_release._write_canonical_zip

    def interrupted_writer(target: Path, members: dict[str, bytes]) -> str:
        writer_sha = original_writer(target, members)
        Path(target).write_bytes(b"truncated-unverified-candidate")
        return writer_sha

    monkeypatch.setattr(canonical_release, "_write_canonical_zip", interrupted_writer)
    with pytest.raises(ValueError, match="changed after canonical publication"):
        stage_release.repackage_stage_neutral_windows_release(
            legacy, output, expected_source_sha=SOURCE_SHA
        )
    assert output.read_bytes() == previous_verified_zip
    assert list(output.parent.glob(f".{output.name}.*.tmp")) == []


def test_stage_neutral_candidate_is_verified_before_replacing_prior_zip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_deep_verifier(monkeypatch)
    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    output = tmp_path / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    _write_legacy_package(legacy)
    stage_release.repackage_stage_neutral_windows_release(
        legacy, output, expected_source_sha=SOURCE_SHA
    )
    previous_verified_zip = output.read_bytes()
    verify_original = stage_release._verify_stage_neutral_output

    def verify_before_publish(package_zip: Path, **kwargs: object) -> dict[str, object]:
        assert Path(package_zip) != output
        assert output.read_bytes() == previous_verified_zip
        return verify_original(package_zip, **kwargs)

    monkeypatch.setattr(stage_release, "_verify_stage_neutral_output", verify_before_publish)
    evidence = stage_release.repackage_stage_neutral_windows_release(
        legacy, output, expected_source_sha=SOURCE_SHA
    )
    assert evidence["package_sha256"] == _sha256(output.read_bytes())
    assert list(output.parent.glob(f".{output.name}.*.tmp")) == []


def test_stage_neutral_failed_atomic_promotion_preserves_previous_zip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_deep_verifier(monkeypatch)
    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    output = tmp_path / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    _write_legacy_package(legacy)
    stage_release.repackage_stage_neutral_windows_release(
        legacy, output, expected_source_sha=SOURCE_SHA
    )
    previous_verified_zip = output.read_bytes()
    actual_replace = stage_release.os.replace

    def fail_final_promotion(source: Path, destination: Path) -> None:
        if Path(destination) == output:
            raise OSError("injected final ZIP promotion interruption")
        actual_replace(source, destination)

    monkeypatch.setattr(stage_release.os, "replace", fail_final_promotion)
    with pytest.raises(OSError, match="injected final ZIP promotion interruption"):
        stage_release.repackage_stage_neutral_windows_release(
            legacy, output, expected_source_sha=SOURCE_SHA
        )
    assert output.read_bytes() == previous_verified_zip
    assert list(output.parent.glob(f".{output.name}.*.tmp")) == []


@pytest.mark.parametrize("protected", ["input", "output"])
def test_cli_rejects_evidence_aliasing_package_before_any_mutation(
    tmp_path: Path, protected: str,
) -> None:
    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    output = tmp_path / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    _write_legacy_package(legacy)
    prior_input = legacy.read_bytes()
    aliased_receipt = legacy if protected == "input" else output
    with pytest.raises(ValueError, match="release evidence must not alias"):
        stage_release.main([
            "--input", str(legacy), "--output", str(output),
            "--source-sha", SOURCE_SHA, "--evidence", str(aliased_receipt),
        ])
    assert legacy.read_bytes() == prior_input
    assert not output.exists()


def test_cli_rejects_existing_output_hardlink_evidence_before_mutation(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    output = tmp_path / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    receipt = tmp_path / "receipt.json"
    _write_legacy_package(legacy)
    output.write_bytes(b"previous-verified-output")
    try:
        os.link(output, receipt)
    except OSError as exc:
        pytest.skip(f"hardlinks are not available in this environment: {exc}")
    with pytest.raises(ValueError, match="release evidence must not alias"):
        stage_release.main([
            "--input", str(legacy), "--output", str(output),
            "--source-sha", SOURCE_SHA, "--evidence", str(receipt),
        ])
    assert output.read_bytes() == b"previous-verified-output"


def test_cli_rejects_dangling_unicode_symlink_receipt_to_future_output(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    output = tmp_path / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    receipt = tmp_path / "звіт з пробілами.json"
    _write_legacy_package(legacy)
    try:
        receipt.symlink_to(output)
    except OSError as exc:
        pytest.skip(f"symlinks are not available in this environment: {exc}")
    with pytest.raises(ValueError, match="release evidence must not alias"):
        stage_release.main([
            "--input", str(legacy), "--output", str(output),
            "--source-sha", SOURCE_SHA, "--evidence", str(receipt),
        ])
    assert not output.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive filesystem")
def test_cli_rejects_windows_casefold_receipt_alias(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / stage_release.LEGACY_ARCHIVE_NAME
    output = tmp_path / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME
    receipt = tmp_path / stage_release.STAGE_NEUTRAL_ARCHIVE_NAME.upper()
    _write_legacy_package(legacy)
    with pytest.raises(ValueError, match="release evidence must not alias"):
        stage_release.main([
            "--input", str(legacy), "--output", str(output),
            "--source-sha", SOURCE_SHA, "--evidence", str(receipt),
        ])
    assert not output.exists()
