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
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from autosport.integration_protected_tree import (  # noqa: E402
    GitTreeEntry,
    ProtectedTreeGateResult,
    TrustedProtectedTreePolicy,
    verify_base_trusted_protected_tree,
)

_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_POLICY_PATH = Path(".github/protected-tree-policy.json")
_WORKFLOW_PREFIX = ".github/workflows/"
_TRUST_ROOT_PATHS = frozenset(
    {
        ".gitattributes",
        ".github/protected-tree-policy.json",
        "pyproject.toml",
        "scripts/build_windows.ps1",
        "scripts/build_windows_candidate.ps1",
        "scripts/evidence_export_package_smoke.ps1",
        "scripts/external_uia_audit.ps1",
        "scripts/nvda_evidence_package_smoke.ps1",
        "scripts/package_windows.py",
        "scripts/packaged_executable_authority.ps1",
        "scripts/verify_protected_tree_gate.py",
        "scripts/verify_source_checkout.py",
        "scripts/walk_forward_origin_package_smoke.ps1",
        "scripts/walk_forward_package_smoke.ps1",
        "src/autosport/integration_protected_tree.py",
        "tests/test_integration_protected_tree.py",
        "tests/test_protected_tree_gate_runner.py",
    }
)
_POLICY_KEYS = frozenset(
    {
        "schema_version",
        "protected_paths",
        "protected_prefixes",
        "content_change_allowlist",
        "immutable_policy_paths",
    }
)


class ProtectedTreeRunnerError(ValueError):
    """Base-trusted Gate-A wiring rejected malformed or ambiguous evidence."""


class _DuplicateJsonKeyError(ValueError):
    pass


def _require_git_sha(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _GIT_SHA_RE.fullmatch(value) is None:
        raise ProtectedTreeRunnerError(
            f"{field} must be a canonical 40-character lowercase Git SHA-1"
        )
    return value


def _require_pr_number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProtectedTreeRunnerError("pr_number must be a positive integer")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(key)
        result[key] = value
    return result


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _string_tuple(payload: dict[str, Any], field: str) -> tuple[str, ...]:
    value = payload.get(field)
    if not isinstance(value, list) or not all(type(item) is str for item in value):
        raise ProtectedTreeRunnerError(f"{field} must be a JSON array of strings")
    return tuple(value)


def _decode_policy_bytes(payload: bytes) -> TrustedProtectedTreePolicy:
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except _DuplicateJsonKeyError as exc:
        raise ProtectedTreeRunnerError(
            f"protected-tree policy contains duplicate key: {exc.args[0]}"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtectedTreeRunnerError(
            "protected-tree policy must be canonical UTF-8 JSON"
        ) from exc
    if not isinstance(decoded, dict) or set(decoded) != _POLICY_KEYS:
        raise ProtectedTreeRunnerError(
            "protected-tree policy must contain exactly the canonical schema keys"
        )
    if type(decoded.get("schema_version")) is not int or decoded["schema_version"] != 1:
        raise ProtectedTreeRunnerError("protected-tree policy schema_version must be 1")

    policy = TrustedProtectedTreePolicy(
        protected_paths=_string_tuple(decoded, "protected_paths"),
        protected_prefixes=_string_tuple(decoded, "protected_prefixes"),
        content_change_allowlist=_string_tuple(decoded, "content_change_allowlist"),
        immutable_policy_paths=_string_tuple(decoded, "immutable_policy_paths"),
    )
    _validate_policy_wiring(policy)
    return policy


def _validate_policy_wiring(policy: TrustedProtectedTreePolicy) -> None:
    if _POLICY_PATH.as_posix() not in policy.immutable_policy_paths:
        raise ProtectedTreeRunnerError(
            "trusted policy file must be explicitly immutable"
        )
    if _WORKFLOW_PREFIX not in policy.protected_prefixes:
        raise ProtectedTreeRunnerError(
            "all GitHub workflow paths must be protected by the trusted base"
        )
    allowlisted = set(policy.content_change_allowlist)
    for path in sorted(_TRUST_ROOT_PATHS):
        if not policy.protects(path):
            raise ProtectedTreeRunnerError(
                f"base-trusted wiring path is not protected: {path}"
            )
        if path in allowlisted:
            raise ProtectedTreeRunnerError(
                f"base-trusted wiring path cannot be content-change allowlisted: {path}"
            )


def _git_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.upper().startswith("GIT_"):
            env.pop(name, None)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def _git_bytes(*args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            env=_git_environment(),
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ProtectedTreeRunnerError(
            "unable to resolve trusted Git evidence: git " + " ".join(args)
        ) from exc
    return completed.stdout


def _git_text(*args: str) -> str:
    try:
        return _git_bytes(*args).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ProtectedTreeRunnerError(
            "trusted Git identity output is not UTF-8"
        ) from exc


def _require_repository_context(base_sha: str) -> None:
    try:
        top = Path(_git_text("rev-parse", "--show-toplevel")).resolve(strict=True)
        repo = _REPO_ROOT.resolve(strict=True)
    except OSError as exc:
        raise ProtectedTreeRunnerError(
            "unable to resolve trusted repository context"
        ) from exc
    if top != repo:
        raise ProtectedTreeRunnerError(
            "Gate-A runner is not executing from the trusted repository root"
        )

    replace_refs = _git_bytes(
        "for-each-ref", "--format=%(refname)", "refs/replace"
    )
    if replace_refs.strip():
        raise ProtectedTreeRunnerError(
            "Git replacement refs are forbidden in Gate-A evidence"
        )

    head = _require_git_sha(
        _git_text("rev-parse", "--verify", "HEAD"),
        field="trusted_base_checkout_head",
    )
    if head != base_sha:
        raise ProtectedTreeRunnerError(
            "checked-out trusted base does not match exact base_sha"
        )

    dirty = _git_bytes(
        "status", "--porcelain=v1", "-z", "--untracked-files=all"
    )
    if dirty:
        raise ProtectedTreeRunnerError(
            "trusted base checkout is not pristine before Gate-A evaluation"
        )


def _parse_ls_tree(payload: bytes, *, label: str) -> tuple[GitTreeEntry, ...]:
    entries: list[GitTreeEntry] = []
    for record in payload.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            raw_mode, raw_type, raw_oid = metadata.split(b" ", 2)
            path = raw_path.decode("utf-8", errors="strict")
            mode = raw_mode.decode("ascii", errors="strict")
            object_type = raw_type.decode("ascii", errors="strict")
            object_id = raw_oid.decode("ascii", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProtectedTreeRunnerError(
                f"{label} contains malformed/non-UTF-8 Git tree entry"
            ) from exc
        entries.append(
            GitTreeEntry(
                path=path,
                mode=mode,
                object_type=object_type,
                object_id=object_id,
            )
        )
    if not entries:
        raise ProtectedTreeRunnerError(f"{label} must not be empty")
    return tuple(entries)


def _git_tree_entries(commit_sha: str, *, label: str) -> tuple[GitTreeEntry, ...]:
    _require_git_sha(commit_sha, field=f"{label}_sha")
    try:
        _git_bytes("cat-file", "-e", f"{commit_sha}^{{commit}}")
    except ProtectedTreeRunnerError as exc:
        raise ProtectedTreeRunnerError(
            f"{label} commit object is unavailable"
        ) from exc
    return _parse_ls_tree(
        _git_bytes("ls-tree", "-r", "-z", "--full-tree", commit_sha),
        label=label,
    )


def _read_trusted_policy() -> TrustedProtectedTreePolicy:
    path = _REPO_ROOT / _POLICY_PATH
    try:
        if path.is_symlink() or not path.is_file():
            raise ProtectedTreeRunnerError(
                "trusted protected-tree policy must be a regular base file"
            )
        payload = path.read_bytes()
    except OSError as exc:
        raise ProtectedTreeRunnerError(
            "unable to read trusted protected-tree policy"
        ) from exc
    return _decode_policy_bytes(payload)


def _validate_github_event(
    *,
    base_sha: str,
    candidate_sha: str,
    pr_number: int,
) -> tuple[str, str]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return repository or "local-test", "local-test"
    if not event_path or not repository:
        raise ProtectedTreeRunnerError(
            "GitHub Gate-A run lacks authoritative event/repository identity"
        )
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtectedTreeRunnerError(
            "GitHub Gate-A event payload is unreadable"
        ) from exc
    if not isinstance(event, dict):
        raise ProtectedTreeRunnerError("GitHub Gate-A event must be a JSON object")
    event_repo = event.get("repository")
    if not isinstance(event_repo, dict) or event_repo.get("full_name") != repository:
        raise ProtectedTreeRunnerError(
            "GitHub Gate-A repository identity mismatch"
        )
    default_branch = event_repo.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch.strip():
        raise ProtectedTreeRunnerError(
            "GitHub Gate-A repository default branch identity is missing"
        )
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ProtectedTreeRunnerError(
            "GitHub Gate-A requires a pull request event"
        )
    if pull_request.get("number") != pr_number:
        raise ProtectedTreeRunnerError(
            "GitHub Gate-A pull request number mismatch"
        )
    base = pull_request.get("base")
    head = pull_request.get("head")
    if (
        not isinstance(base, dict)
        or not isinstance(head, dict)
        or base.get("sha") != base_sha
        or head.get("sha") != candidate_sha
    ):
        raise ProtectedTreeRunnerError(
            "GitHub Gate-A base/head SHA does not match authoritative event"
        )
    base_repo = base.get("repo")
    if not isinstance(base_repo, dict) or base_repo.get("full_name") != repository:
        raise ProtectedTreeRunnerError(
            "GitHub Gate-A base repository does not match authoritative repository"
        )
    if base.get("ref") != default_branch:
        raise ProtectedTreeRunnerError(
            "GitHub Gate-A PR base must be the repository default branch"
        )
    return repository, default_branch


def _build_wrapper_evidence(
    *,
    repository: str,
    pr_number: int,
    base_ref: str,
    base_sha: str,
    candidate_sha: str,
    result: ProtectedTreeGateResult,
) -> dict[str, Any]:
    if (
        type(result) is not ProtectedTreeGateResult
        or result.status != "PASS"
        or result.candidate_ci_eligible is not True
        or result.merge_authorized is not False
        or result.release_authorized is not False
    ):
        raise ProtectedTreeRunnerError(
            "Gate-A result is not an exact candidate-CI-only PASS"
        )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": "PASS",
        "repository": repository,
        "pull_request_number": _require_pr_number(pr_number),
        "base_ref": base_ref,
        "base_sha": _require_git_sha(base_sha, field="base_sha"),
        "candidate_sha": _require_git_sha(candidate_sha, field="candidate_sha"),
        "policy_path": _POLICY_PATH.as_posix(),
        "protected_entry_count": result.protected_entry_count,
        "allowed_content_changes": list(result.allowed_content_changes),
        "gate_evidence_sha256": result.evidence_sha256,
        "candidate_ci_eligible": True,
        "merge_authorized": False,
        "release_authorized": False,
    }
    payload["wrapper_evidence_sha256"] = hashlib.sha256(
        _canonical_json_bytes(payload)
    ).hexdigest()
    return payload


def _write_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_canonical_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def evaluate(
    *,
    base_sha: str,
    candidate_sha: str,
    pr_number: int,
    evidence_output: Path,
) -> dict[str, Any]:
    base_sha = _require_git_sha(base_sha, field="base_sha")
    candidate_sha = _require_git_sha(candidate_sha, field="candidate_sha")
    pr_number = _require_pr_number(pr_number)
    repository, base_ref = _validate_github_event(
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        pr_number=pr_number,
    )

    _require_repository_context(base_sha)
    policy = _read_trusted_policy()
    base_tree = _git_tree_entries(base_sha, label="trusted_base")
    candidate_tree = _git_tree_entries(candidate_sha, label="candidate")
    trusted_manifest = tuple(
        entry for entry in base_tree if policy.protects(entry.path)
    )
    result = verify_base_trusted_protected_tree(
        base_tree=base_tree,
        candidate_tree=candidate_tree,
        trusted_base_manifest=trusted_manifest,
        policy=policy,
    )
    _require_repository_context(base_sha)

    evidence = _build_wrapper_evidence(
        repository=repository,
        pr_number=pr_number,
        base_ref=base_ref,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        result=result,
    )
    _write_evidence(evidence_output, evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument(
        "--evidence-output",
        required=True,
        type=Path,
    )
    args = parser.parse_args()

    evidence = evaluate(
        base_sha=args.base_sha,
        candidate_sha=args.candidate_sha,
        pr_number=args.pr_number,
        evidence_output=args.evidence_output,
    )
    print("PROTECTED_TREE_GATE=PASS")
    print(f"PROTECTED_TREE_GATE_EVIDENCE_SHA256={evidence['wrapper_evidence_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
