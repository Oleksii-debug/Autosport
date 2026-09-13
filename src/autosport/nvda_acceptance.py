from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_KIND = "physical_nvda_acceptance"
_BUILD_INFO_MEMBER = "Autosport-V1/BUILD_INFO.json"
_EXE_MEMBER = "Autosport-V1/Autosport.exe"
_REQUIRED_CHECKS = (
    ("window_initial_focus", "Main window and initial focus are announced clearly by NVDA."),
    ("primary_tab_flow", "Tab/Shift+Tab primary flow exposes name, role and state without traps."),
    ("baseline_replay", "Packaged baseline replay state/completion/error is available without visual reading."),
    ("evidence_surfaces", "F6/F7/F8 ticket, live quote and Evaluation surfaces receive predictable accessible focus."),
    ("research_missing_plan_error", "Research replay without a plan fails closed with an NVDA-accessible reason."),
    ("restart_persistence", "Restart returns to the same economic context without silent state loss/substitution."),
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_candidate_identity(release_zip: str | Path) -> dict[str, str]:
    release_zip = Path(release_zip)
    package_sha = sha256_file(release_zip)
    with zipfile.ZipFile(release_zip, "r") as archive:
        names = [item.filename for item in archive.infolist() if not item.is_dir()]
        if len(names) != len(set(names)):
            raise ValueError("release ZIP contains duplicate members")
        for required in (_BUILD_INFO_MEMBER, _EXE_MEMBER):
            if required not in names:
                raise ValueError(f"release ZIP is missing {required}")
        try:
            build_info = json.loads(archive.read(_BUILD_INFO_MEMBER).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("release BUILD_INFO.json is not valid UTF-8 JSON") from exc
        if not isinstance(build_info, dict):
            raise ValueError("release BUILD_INFO.json must contain an object")
        if build_info.get("product") != "Autosport":
            raise ValueError("release BUILD_INFO product identity mismatch")
        for key in ("real_money_execution", "human_tested", "nvda_verified"):
            if build_info.get(key) is not False:
                raise ValueError(f"release BUILD_INFO must preserve {key}=false")
        source_sha = build_info.get("source_sha")
        exe_sha = build_info.get("autosport_exe_sha256")
        if not isinstance(source_sha, str) or not source_sha:
            raise ValueError("release BUILD_INFO source_sha is missing")
        if not isinstance(exe_sha, str) or len(exe_sha) != 64:
            raise ValueError("release BUILD_INFO autosport_exe_sha256 is invalid")
        try:
            int(exe_sha, 16)
        except ValueError as exc:
            raise ValueError("release BUILD_INFO autosport_exe_sha256 is not hexadecimal") from exc
        actual_exe_sha = _sha256_bytes(archive.read(_EXE_MEMBER))
        if actual_exe_sha != exe_sha.lower():
            raise ValueError("release Autosport.exe hash does not match BUILD_INFO")
    return {
        "package_sha256": package_sha,
        "source_sha": source_sha,
        "autosport_exe_sha256": exe_sha.lower(),
    }


def create_template(release_zip: str | Path) -> dict[str, Any]:
    identity = _load_candidate_identity(release_zip)
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
            "Machine validation checks structure and exact candidate identity only."
        ),
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
        "v1_ready": False,
    }


def write_template(release_zip: str | Path, output: str | Path) -> dict[str, Any]:
    payload = create_template(release_zip)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _read_evidence(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("NVDA acceptance evidence is not readable UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("NVDA acceptance evidence must contain an object")
    return payload


def validate_evidence(release_zip: str | Path, evidence_path: str | Path) -> dict[str, Any]:
    identity = _load_candidate_identity(release_zip)
    evidence = _read_evidence(evidence_path)
    if evidence.get("schema_version") != _SCHEMA_VERSION or evidence.get("kind") != _KIND:
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
    for check_id, _description in _REQUIRED_CHECKS:
        item = by_id[check_id]
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


def template_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create exact-candidate physical NVDA acceptance evidence template.")
    parser.add_argument("--release-zip", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = write_template(args.release_zip, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"NVDA_EVIDENCE_TEMPLATE_INVALID={exc}", file=sys.stderr)
        return 2
    print(f"NVDA_EVIDENCE_TEMPLATE={args.output}")
    print(f"PACKAGE_SHA256={payload['candidate']['package_sha256']}")
    print("HUMAN_TESTED=false")
    print("NVDA_VERIFIED=false")
    return 0


def verify_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate human NVDA evidence against one exact Autosport release ZIP.")
    parser.add_argument("--release-zip", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = validate_evidence(args.release_zip, args.evidence)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"NVDA_EVIDENCE_INVALID={exc}", file=sys.stderr)
        return 2
    if args.output is not None:
        _write_json(args.output, result)
    print(f"NVDA_ACCEPTANCE={result['status']}")
    print(f"PACKAGE_SHA256={result['package_sha256']}")
    print("CANDIDATE_IDENTITY_VERIFIED=true")
    print("MACHINE_VERIFIED_PHYSICAL_EXECUTION=false")
    print("NVDA_VERIFIED=false")
    return 0 if result["status"] == "PASS" else 3
