"""Exact-revision, no-secret-output QA qualification for Plan 5 Section 4.

This is a read-only orchestrator over existing canonical regression tests. Its
report is test evidence, never authority for execution, strategy or promotion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE_SHA = re.compile(r"[0-9a-f]{40}\\Z")
MAX_TEST_FILE_BYTES = 4 * 1024 * 1024
AREAS: dict[str, tuple[str, ...]] = {
    "causal": (
        "tests/test_decision_ledger_future_key_integrity.py",
        "tests/test_market_event_causal_timestamp_deserialization.py",
    ),
    "financial": (
        "tests/test_paperbook_risk_generation_guard.py",
        "tests/test_portfolio_decimal_context_integrity.py",
    ),
    "provider": (
        "tests/test_provider_observation_authority_forgery.py",
        "tests/test_provider_source_identity_integrity.py",
    ),
    "recovery": (
        "tests/test_transaction_recovery.py",
        "tests/test_product_runtime_caller_lease_release_falsifier.py",
    ),
    "agent": (
        "tests/test_agent_composition_identity.py",
        "tests/test_learning_environment_reward_causality.py",
    ),
    "security": (
        "tests/test_secret_canary_scan.py",
        "tests/test_forensic_session_journal_lock_publication.py",
    ),
}


class MatrixError(ValueError):
    """QA input or source evidence cannot be trusted."""


def _sha(value: str) -> str:
    if type(value) is not str or SOURCE_SHA.fullmatch(value) is None:
        raise MatrixError("invalid exact source SHA")
    return value


def _read_fixture(root: Path, relative: str) -> str:
    if not relative.startswith("tests/") or ".." in Path(relative).parts:
        raise MatrixError("invalid test path")
    path = root / relative
    try:
        if path.is_symlink() or not path.is_file():
            raise MatrixError("missing or aliased QA test")
        stat = path.stat()
        if stat.st_size <= 0 or stat.st_size > MAX_TEST_FILE_BYTES:
            raise MatrixError("QA test exceeds source resource bound")
        with path.open("rb") as handle:
            body = handle.read(MAX_TEST_FILE_BYTES + 1)
        if len(body) != stat.st_size:
            raise MatrixError("QA test changed during source read")
        if len(body) > MAX_TEST_FILE_BYTES:
            raise MatrixError("QA test exceeds source resource bound")
    except OSError as exc:
        raise MatrixError("QA test is not readable") from exc
    return hashlib.sha256(body).hexdigest()


def _assert_checkout(root: Path, source_sha: str) -> None:
    try:
        check = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MatrixError("cannot prove checkout identity") from exc
    if check.returncode or check.stdout.strip() != source_sha:
        raise MatrixError("checkout SHA mismatch")


def _run_group(root: Path, paths: tuple[str, ...]) -> bool:
    try:
        check = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--disable-warnings", *paths],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1800,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return check.returncode == 0


def run_matrix(root: Path, source_sha: str) -> dict[str, object]:
    """Fail closed; bind every reported group to exact source and test bytes."""
    source_sha = _sha(source_sha)
    root = root.resolve(strict=True)
    _assert_checkout(root, source_sha)
    fingerprint: dict[str, dict[str, str]] = {}
    for area, paths in AREAS.items():
        fingerprint[area] = {p: _read_fixture(root, p) for p in paths}
    results: dict[str, str] = {}
    # All files are preflighted before any subprocess runs.
    for area, paths in AREAS.items():
        results[area] = "PASS" if _run_group(root, paths) else "FAIL"
    identity = {
        "schema": "autosport.plan5-systematic-qa",
        "schema_version": 1,
        "source_sha": source_sha,
        "test_sha256": fingerprint,
        "areas": results,
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        **identity,
        "status": "PASS" if all(s == "PASS" for s in results.values()) else "FAIL",
        "evidence_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def _publish(output: Path, payload: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise MatrixError("output alias not permitted")
    data = (json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\\n").encode("utf-8")
    if len(data) > 65536:
        raise MatrixError("evidence exceeds resource bound")
    fd, tmp = tempfile.mkstemp(prefix=".plan5-qa-", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, output)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qualify Plan 5 Section 4 regression families")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", type=Path, default=Path("plan5-qa-evidence.json"))
    args = parser.parse_args(argv)
    output = args.output.resolve(strict=False)
    try:
        # A stale report must not survive an attempted requalification.
        if output.is_symlink():
            raise MatrixError("output alias not permitted")
        output.unlink(missing_ok=True)
        result = run_matrix(args.repo_root, args.source_sha)
        _publish(output, result)
    except (MatrixError, OSError, ValueError) as exc:
        # No input excerpts, exception text, subprocess output or credentials.
        print("plan5_qa=INVALID")
        return 3
    print("plan5_qa=" + str(result["status"]) + " source_sha=" + str(result["source_sha"]))
    return 0 if result["status"] == "PASS" else 5


if __name__ == "__main__":
    raise SystemExit(main())
