from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .data_tool_package import verify_portable_data_tool
from .release_package import verify_windows_package

_SCHEMA_VERSION = 1
_KIND = "physical_nvda_acceptance"
_BUILD_INFO_MEMBER = "Autosport-V1/BUILD_INFO.json"
_EXE_MEMBER = "Autosport-V1/Autosport.exe"
_REQUIRED_CHECKS = (
    ("window_initial_focus", "Main window and initial focus are announced clearly by NVDA."),
    ("primary_tab_flow", "Tab/Shift+Tab primary flow exposes name, role and state without traps."),
    (
        "strategy_replay_settings",
        "Keyboard-only strategy, research-plan, replay-speed and live-mode controls expose current selection/state and validation errors clearly through NVDA.",
    ),
    ("baseline_replay", "Packaged baseline replay state/completion/error is available without visual reading."),
    ("evidence_surfaces", "F6/F7/F8 ticket, live quote and Evaluation surfaces receive predictable accessible focus."),
    ("research_missing_plan_error", "Research replay without a plan fails closed with an NVDA-accessible reason."),
    ("restart_persistence", "Restart returns to the same economic context without silent state loss/substitution."),
)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_hex_digest(value: str, *, length: int, field: str) -> str:
    if not isinstance(value, str) or len(value) != length:
        raise ValueError(f"{field} must be a {length}-character hexadecimal digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{field} must be hexadecimal") from exc
    return value.lower()


def _validate_decoded_json_value(value: Any, *, context: str, path: str = "$") -> None:
    """Reject decoded JSON values that cannot be represented safely and deterministically."""

    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{context} contains a non-finite JSON number at {path}")
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(
                f"{context} contains a string that is not valid UTF-8 Unicode at {path}"
            ) from exc
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_decoded_json_value(item, context=context, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_decoded_json_value(key, context=context, path=f"{path}.<key>")
            _validate_decoded_json_value(item, context=context, path=f"{path}[{key!r}]")
        return
    raise ValueError(f"{context} contains an unsupported decoded JSON value at {path}")


def _strict_json_object_bytes(payload: bytes, *, context: str) -> dict[str, Any]:
    """Parse one trust-boundary JSON object without lossy/ambiguous JSON extensions."""

    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"{context} contains duplicate JSON object key: {key}")
            value[key] = item
        return value

    def _reject_nonstandard_constant(value: str) -> None:
        raise ValueError(f"{context} contains non-standard JSON constant: {value}")

    try:
        decoded = payload.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonstandard_constant,
        )
        _validate_decoded_json_value(value, context=context)
    except RecursionError as exc:
        raise ValueError(f"{context} JSON nesting exceeds parser recursion limit") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{context} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{context} must contain an object")
    return value


def _snapshot_release_zip(release_zip: Path) -> tuple[BinaryIO, str]:
    """Capture one already-open release file into a stable open handle while hashing exact bytes."""

    digest = hashlib.sha256()
    snapshot = tempfile.TemporaryFile(mode="w+b")
    try:
        with release_zip.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                snapshot.write(chunk)
        snapshot.flush()
        os.fsync(snapshot.fileno())
        snapshot.seek(0)
        return snapshot, digest.hexdigest()
    except Exception:
        snapshot.close()
        raise


def _materialize_snapshot_copy(snapshot: BinaryIO) -> Path:
    """Materialize a verifier-only path from the still-open authoritative snapshot handle."""

    fd, temporary_name = tempfile.mkstemp(prefix="autosport-nvda-verifier-", suffix=".zip")
    destination = Path(temporary_name)
    try:
        snapshot.seek(0)
        with os.fdopen(fd, "wb") as output:
            fd = -1
            for chunk in iter(lambda: snapshot.read(1024 * 1024), b""):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        snapshot.seek(0)
        return destination
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        snapshot.seek(0)
        raise


def _verify_snapshot_with_path(
    snapshot: BinaryIO,
    *,
    expected_package_sha256: str,
    label: str,
    verifier: Callable[[Path], dict[str, Any]],
) -> dict[str, Any]:
    """Run a legacy path verifier on a fresh copy and reject any copy mutation/rebinding."""

    candidate = _materialize_snapshot_copy(snapshot)
    try:
        if sha256_file(candidate) != expected_package_sha256:
            raise ValueError(f"{label} snapshot identity mismatch before verification")
        result = verifier(candidate)
        if not isinstance(result, dict):
            raise ValueError(f"{label} did not return verification evidence")
        if sha256_file(candidate) != expected_package_sha256:
            raise ValueError(f"{label} snapshot changed during verification")
        return result
    finally:
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _load_candidate_identity(
    release_zip: str | Path,
    *,
    expected_source_sha: str,
    expected_package_sha256: str,
) -> dict[str, str]:
    """Bind one release ZIP to caller-supplied expected source/package identities.

    The caller-visible release path is opened once. Those exact bytes remain authoritative in
    one still-open private handle, so later validation never trusts the caller pathname again.
    Legacy path-based package verifiers each receive a fresh copy from that handle; the copy is
    hashed before and after the verifier call and returned identity evidence is rebound to the
    original package/source/executable anchors.

    This proves equality to the supplied anchors. It cannot prove where those anchors came
    from; physical-release procedure must obtain them independently from canonical control
    evidence rather than deriving them from the ZIP under test.
    """

    expected_source_sha = _require_hex_digest(
        expected_source_sha,
        length=40,
        field="expected source SHA",
    )
    expected_package_sha256 = _require_hex_digest(
        expected_package_sha256,
        length=64,
        field="expected package SHA-256",
    )
    release_zip = Path(release_zip)
    snapshot: BinaryIO | None = None
    try:
        snapshot, package_sha = _snapshot_release_zip(release_zip)
        if package_sha != expected_package_sha256:
            raise ValueError("release ZIP SHA-256 does not match supplied expected package SHA-256")

        with zipfile.ZipFile(snapshot, "r") as archive:
            names = [item.filename for item in archive.infolist() if not item.is_dir()]
            if len(names) != len(set(names)):
                raise ValueError("release ZIP contains duplicate members")
            for required in (_BUILD_INFO_MEMBER, _EXE_MEMBER):
                if required not in names:
                    raise ValueError(f"release ZIP is missing {required}")
            build_info = _strict_json_object_bytes(
                archive.read(_BUILD_INFO_MEMBER),
                context="release BUILD_INFO.json",
            )
            source_sha = build_info.get("source_sha")
            exe_sha = build_info.get("autosport_exe_sha256")
            if not isinstance(source_sha, str):
                raise ValueError("release BUILD_INFO source_sha is missing")
            source_sha = _require_hex_digest(source_sha, length=40, field="release BUILD_INFO source_sha")
            if source_sha != expected_source_sha:
                raise ValueError("release BUILD_INFO source_sha does not match supplied expected source SHA")
            if not isinstance(exe_sha, str):
                raise ValueError("release BUILD_INFO autosport_exe_sha256 is invalid")
            exe_sha = _require_hex_digest(exe_sha, length=64, field="release BUILD_INFO autosport_exe_sha256")
            if hashlib.sha256(archive.read(_EXE_MEMBER)).hexdigest() != exe_sha:
                raise ValueError("release BUILD_INFO Autosport.exe hash mismatch")

        windows_result = _verify_snapshot_with_path(
            snapshot,
            expected_package_sha256=package_sha,
            label="Windows package verifier",
            verifier=lambda candidate: verify_windows_package(
                candidate,
                expected_source_sha=expected_source_sha,
            ),
        )
        expected_windows_identity = {
            "package_sha256": package_sha,
            "source_sha": source_sha,
            "autosport_exe_sha256": exe_sha,
        }
        for field, expected in expected_windows_identity.items():
            if windows_result.get(field) != expected:
                raise ValueError(f"Windows package verifier {field} does not match captured release identity")

        _verify_snapshot_with_path(
            snapshot,
            expected_package_sha256=package_sha,
            label="portable data-tool verifier",
            verifier=lambda candidate: verify_portable_data_tool(candidate),
        )
        return {
            "package_sha256": package_sha,
            "source_sha": source_sha,
            "autosport_exe_sha256": exe_sha,
        }
    finally:
        if snapshot is not None:
            snapshot.close()


def create_template(
    release_zip: str | Path,
    *,
    expected_source_sha: str,
    expected_package_sha256: str,
) -> dict[str, Any]:
    identity = _load_candidate_identity(
        release_zip,
        expected_source_sha=expected_source_sha,
        expected_package_sha256=expected_package_sha256,
    )
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "candidate": identity,
        "environment": {
            "windows_edition_build": "",
            "nvda_version": "",
        },
        "tested_at": "",
        "tester_label": "",
        "checks": [
            {"id": check_id, "description": description, "status": "PENDING", "notes": ""}
            for check_id, description in _REQUIRED_CHECKS
        ],
        "attestation_scope": (
            "Human-supplied physical Windows 11 + NVDA test record. "
            "Machine validation checks exact candidate identity against caller-supplied "
            "expected source/package anchors. Anchor provenance/independence is not machine-proven."
        ),
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
        "v1_ready": False,
    }


def write_template(
    release_zip: str | Path,
    output: str | Path,
    *,
    expected_source_sha: str,
    expected_package_sha256: str,
) -> dict[str, Any]:
    payload = create_template(
        release_zip,
        expected_source_sha=expected_source_sha,
        expected_package_sha256=expected_package_sha256,
    )
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _read_evidence(path: str | Path) -> dict[str, Any]:
    try:
        payload = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError("NVDA acceptance evidence is not readable UTF-8 JSON") from exc
    try:
        return _strict_json_object_bytes(payload, context="NVDA acceptance evidence")
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def validate_evidence(
    release_zip: str | Path,
    evidence_path: str | Path,
    *,
    expected_source_sha: str,
    expected_package_sha256: str,
) -> dict[str, Any]:
    identity = _load_candidate_identity(
        release_zip,
        expected_source_sha=expected_source_sha,
        expected_package_sha256=expected_package_sha256,
    )
    evidence = _read_evidence(evidence_path)
    schema_version = evidence.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != _SCHEMA_VERSION
        or evidence.get("kind") != _KIND
    ):
        raise ValueError("NVDA acceptance evidence schema/kind mismatch")
    candidate = evidence.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("NVDA acceptance evidence candidate must be an object")
    for key, expected in identity.items():
        if candidate.get(key) != expected:
            raise ValueError(f"NVDA acceptance evidence candidate {key} does not match release ZIP")

    environment = evidence.get("environment")
    if not isinstance(environment, dict):
        raise ValueError("NVDA acceptance evidence environment must be an object")
    for key in ("windows_edition_build", "nvda_version"):
        value = environment.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"NVDA acceptance evidence environment.{key} is required")

    tested_at = evidence.get("tested_at")
    if not isinstance(tested_at, str) or not tested_at.strip():
        raise ValueError("NVDA acceptance evidence tested_at is required")
    try:
        parsed_time = datetime.fromisoformat(tested_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("NVDA acceptance evidence tested_at must be ISO-8601") from exc
    if parsed_time.tzinfo is None:
        raise ValueError("NVDA acceptance evidence tested_at must include a timezone")

    tester_label = evidence.get("tester_label")
    if not isinstance(tester_label, str) or not tester_label.strip():
        raise ValueError("NVDA acceptance evidence tester_label is required")

    checks = evidence.get("checks")
    if not isinstance(checks, list):
        raise ValueError("NVDA acceptance evidence checks must be a list")
    by_id: dict[str, dict[str, Any]] = {}
    for item in checks:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("NVDA acceptance evidence contains a malformed check")
        check_id = item["id"]
        if check_id in by_id:
            raise ValueError(f"NVDA acceptance evidence contains duplicate check {check_id}")
        by_id[check_id] = item
    expected_ids = {check_id for check_id, _description in _REQUIRED_CHECKS}
    if set(by_id) != expected_ids:
        missing = sorted(expected_ids.difference(by_id))
        extra = sorted(set(by_id).difference(expected_ids))
        raise ValueError(f"NVDA acceptance evidence check set mismatch; missing={missing}, extra={extra}")

    failed: list[str] = []
    for check_id, expected_description in _REQUIRED_CHECKS:
        item = by_id[check_id]
        if item.get("description") != expected_description:
            raise ValueError(
                f"NVDA acceptance evidence check {check_id} description does not match the canonical physical test contract"
            )
        status = item.get("status")
        if status not in {"PASS", "FAIL"}:
            raise ValueError(f"NVDA acceptance evidence check {check_id} must be PASS or FAIL")
        if status == "FAIL":
            notes = item.get("notes")
            if not isinstance(notes, str) or not notes.strip():
                raise ValueError(f"NVDA acceptance evidence failed check {check_id} requires defect notes")
            failed.append(check_id)

    for key in ("real_money_execution", "human_tested", "nvda_verified", "v1_ready"):
        if evidence.get(key) is not False:
            raise ValueError(f"evidence record must preserve {key}=false; this validator never promotes release truth")

    status = "PASS" if not failed else "FAIL"
    return {
        "status": status,
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "candidate_identity_verified": True,
        "source_anchor_match_verified": True,
        "package_anchor_match_verified": True,
        "anchor_provenance_machine_verified": False,
        **identity,
        "windows_edition_build": environment["windows_edition_build"].strip(),
        "nvda_version": environment["nvda_version"].strip(),
        "tested_at": tested_at,
        "tester_label": tester_label.strip(),
        "required_check_count": len(_REQUIRED_CHECKS),
        "failed_checks": failed,
        "machine_verified_physical_execution": False,
        "requires_owner_release_decision": True,
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
        "v1_ready": False,
    }


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _add_trust_anchor_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--expected-source-sha",
        required=True,
        metavar="GIT_SHA",
        help=(
            "канонічний 40-символьний Git SHA вихідного коду для цього релізу; "
            "отримайте його незалежно від ZIP-файлу релізу"
        ),
    )
    parser.add_argument(
        "--expected-package-sha256",
        required=True,
        metavar="SHA256_ZIP",
        help=(
            "64-символьний SHA-256 ZIP-файлу релізу; отримайте його незалежно "
            "від самого ZIP-файлу релізу"
        ),
    )


def _template_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Створює шаблон доказу фізичного тестування NVDA, прив'язаний "
            "до точного кандидата релізу Autosport."
        ),
    )
    parser.add_argument(
        "--release-zip",
        type=Path,
        required=True,
        metavar="ZIP_РЕЛІЗУ",
        help="ZIP-файл точного кандидата релізу Autosport",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        metavar="JSON_ШАБЛОН",
        help="новий JSON-файл шаблону фізичного тестування NVDA",
    )
    _add_trust_anchor_arguments(parser)
    return parser


def template_main(argv: list[str] | None = None) -> int:
    args = _template_parser().parse_args(argv)
    try:
        payload = write_template(
            args.release_zip,
            args.output,
            expected_source_sha=args.expected_source_sha,
            expected_package_sha256=args.expected_package_sha256,
        )
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"NVDA_EVIDENCE_TEMPLATE_INVALID={exc}", file=sys.stderr)
        return 2
    print(f"NVDA_EVIDENCE_TEMPLATE={args.output}")
    print(f"PACKAGE_SHA256={payload['candidate']['package_sha256']}")
    print("SOURCE_ANCHOR_MATCH_VERIFIED=true")
    print("PACKAGE_ANCHOR_MATCH_VERIFIED=true")
    print("ANCHOR_PROVENANCE_MACHINE_VERIFIED=false")
    print("HUMAN_TESTED=false")
    print("NVDA_VERIFIED=false")
    return 0


def _verify_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Перевіряє заповнений людиною доказ NVDA щодо одного точного "
            "ZIP-релізу Autosport."
        ),
    )
    parser.add_argument(
        "--release-zip",
        type=Path,
        required=True,
        metavar="ZIP_РЕЛІЗУ",
        help="ZIP-файл точного кандидата релізу Autosport",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        required=True,
        metavar="JSON_ДОКАЗ",
        help="JSON-файл доказу фізичного тестування NVDA, заповнений людиною",
    )
    parser.add_argument(
        "--output",
        type=Path,
        metavar="JSON_ЗВІТ",
        help="необов'язковий JSON-файл машинного звіту перевірки",
    )
    _add_trust_anchor_arguments(parser)
    return parser


def verify_main(argv: list[str] | None = None) -> int:
    args = _verify_parser().parse_args(argv)
    try:
        result = validate_evidence(
            args.release_zip,
            args.evidence,
            expected_source_sha=args.expected_source_sha,
            expected_package_sha256=args.expected_package_sha256,
        )
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"NVDA_EVIDENCE_INVALID={exc}", file=sys.stderr)
        return 2
    if args.output is not None:
        _write_json(args.output, result)
    print(f"NVDA_EVIDENCE_CHECKS={result['status']}")
    print(f"PACKAGE_SHA256={result['package_sha256']}")
    print("CANDIDATE_IDENTITY_VERIFIED=true")
    print("SOURCE_ANCHOR_MATCH_VERIFIED=true")
    print("PACKAGE_ANCHOR_MATCH_VERIFIED=true")
    print("ANCHOR_PROVENANCE_MACHINE_VERIFIED=false")
    print("MACHINE_VERIFIED_PHYSICAL_EXECUTION=false")
    print("NVDA_VERIFIED=false")
    return 0 if result["status"] == "PASS" else 3
