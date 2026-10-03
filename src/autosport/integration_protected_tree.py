from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from typing import Iterable

_HEX = frozenset("0123456789abcdef")
_ALLOWED_ENTRY_SHAPES = {
    ("100644", "blob"),
    ("100755", "blob"),
    ("120000", "blob"),
    ("160000", "commit"),
}


class ProtectedTreeGateError(ValueError):
    """Trusted-base protected-tree gate rejected malformed or unsafe evidence."""


@dataclass(frozen=True, slots=True)
class GitTreeEntry:
    path: str
    mode: str
    object_type: str
    object_id: str

    def __post_init__(self) -> None:
        _canonical_path(self.path)
        if (self.mode, self.object_type) not in _ALLOWED_ENTRY_SHAPES:
            raise ProtectedTreeGateError(
                f"unsupported Git entry shape for {self.path}: "
                f"{self.mode}/{self.object_type}"
            )
        if (
            type(self.object_id) is not str
            or len(self.object_id) != 40
            or any(char not in _HEX for char in self.object_id)
        ):
            raise ProtectedTreeGateError(
                f"object_id for {self.path} must be canonical lowercase Git SHA-1"
            )


@dataclass(frozen=True, slots=True)
class TrustedProtectedTreePolicy:
    protected_paths: tuple[str, ...]
    protected_prefixes: tuple[str, ...] = ()
    content_change_allowlist: tuple[str, ...] = ()
    immutable_policy_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "protected_paths",
            "content_change_allowlist",
            "immutable_policy_paths",
        ):
            value = getattr(self, field_name)
            if type(value) is not tuple:
                raise ProtectedTreeGateError(f"{field_name} must be a tuple")
            canonical = tuple(_canonical_path(item) for item in value)
            if canonical != value:
                raise ProtectedTreeGateError(
                    f"{field_name} must contain canonical paths"
                )
            if len(canonical) != len(set(canonical)):
                raise ProtectedTreeGateError(
                    f"{field_name} must not contain duplicates"
                )

        if type(self.protected_prefixes) is not tuple:
            raise ProtectedTreeGateError("protected_prefixes must be a tuple")
        canonical_prefixes = tuple(
            _canonical_prefix(item) for item in self.protected_prefixes
        )
        if canonical_prefixes != self.protected_prefixes:
            raise ProtectedTreeGateError(
                "protected_prefixes must contain canonical prefixes"
            )
        if len(canonical_prefixes) != len(set(canonical_prefixes)):
            raise ProtectedTreeGateError(
                "protected_prefixes must not contain duplicates"
            )
        if not (
            self.protected_paths
            or self.protected_prefixes
            or self.immutable_policy_paths
        ):
            raise ProtectedTreeGateError(
                "trusted protected-tree policy must protect at least one path or prefix"
            )

        effective_immutable = set(self.immutable_policy_paths)
        if effective_immutable.intersection(self.content_change_allowlist):
            raise ProtectedTreeGateError(
                "immutable policy paths cannot be content-change allowlisted"
            )

        for path in self.content_change_allowlist:
            if not self.protects(path):
                raise ProtectedTreeGateError(
                    "content-change allowlist path must already be protected"
                )

    def protects(self, path: str) -> bool:
        canonical = _canonical_path(path)
        return (
            canonical in self.protected_paths
            or canonical in self.immutable_policy_paths
            or any(canonical.startswith(prefix) for prefix in self.protected_prefixes)
        )


@dataclass(frozen=True, slots=True)
class ProtectedTreeGateResult:
    status: str
    protected_entry_count: int
    allowed_content_changes: tuple[str, ...]
    evidence_sha256: str
    candidate_ci_eligible: bool
    merge_authorized: bool = False
    release_authorized: bool = False


def verify_base_trusted_protected_tree(
    *,
    base_tree: Iterable[GitTreeEntry],
    candidate_tree: Iterable[GitTreeEntry],
    trusted_base_manifest: Iterable[GitTreeEntry],
    policy: TrustedProtectedTreePolicy,
) -> ProtectedTreeGateResult:
    """Gate candidate CI on a policy and protected manifest sourced from trusted base.

    This deliberately does not authorize merge or release. It only proves that the
    candidate preserved the trusted base's protected tree except for base-trusted
    content-only allowlist entries.
    """

    if type(policy) is not TrustedProtectedTreePolicy:
        raise ProtectedTreeGateError(
            "policy must be exact TrustedProtectedTreePolicy"
        )

    base = _index_tree(base_tree, label="trusted base tree")
    candidate = _index_tree(candidate_tree, label="candidate tree")
    manifest = _index_tree(
        trusted_base_manifest, label="trusted protected manifest"
    )

    expected_protected = {
        path: entry for path, entry in base.items() if policy.protects(path)
    }
    if not expected_protected:
        raise ProtectedTreeGateError(
            "trusted protected-tree policy selects no paths from trusted base"
        )

    declared_missing_from_base = sorted(
        path
        for path in (*policy.protected_paths, *policy.immutable_policy_paths)
        if path not in base
    )
    if declared_missing_from_base:
        raise ProtectedTreeGateError(
            "trusted policy names protected path absent from trusted base: "
            + ", ".join(declared_missing_from_base)
        )

    if set(manifest) != set(expected_protected):
        missing = sorted(set(expected_protected).difference(manifest))
        extra = sorted(set(manifest).difference(expected_protected))
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise ProtectedTreeGateError(
            "trusted protected manifest is incomplete or out of scope"
            + (": " + "; ".join(details) if details else "")
        )

    for path, expected in expected_protected.items():
        if manifest[path] != expected:
            raise ProtectedTreeGateError(
                f"trusted protected manifest does not match trusted base: {path}"
            )

    candidate_protected = {
        path: entry for path, entry in candidate.items() if policy.protects(path)
    }
    newly_protected = sorted(set(candidate_protected).difference(expected_protected))
    if newly_protected:
        raise ProtectedTreeGateError(
            "candidate adds untrusted path inside protected tree: "
            + ", ".join(newly_protected)
        )

    allowed_changes: list[str] = []
    for path, base_entry in expected_protected.items():
        candidate_entry = candidate.get(path)
        if candidate_entry is None:
            raise ProtectedTreeGateError(
                f"candidate deletes protected path: {path}"
            )

        if path in policy.content_change_allowlist:
            if (
                candidate_entry.mode != base_entry.mode
                or candidate_entry.object_type != base_entry.object_type
            ):
                raise ProtectedTreeGateError(
                    f"allowlisted path changes Git entry kind: {path}"
                )
            if candidate_entry.object_id != base_entry.object_id:
                if base_entry.mode in {"120000", "160000"}:
                    raise ProtectedTreeGateError(
                        f"allowlisted special Git entry changes target/commit: {path}"
                    )
                allowed_changes.append(path)
            continue

        if candidate_entry != base_entry:
            raise ProtectedTreeGateError(
                f"candidate modifies protected path without base-trusted allowlist: {path}"
            )

    evidence = {
        "schema_version": 1,
        "policy": {
            "protected_paths": sorted(policy.protected_paths),
            "protected_prefixes": sorted(policy.protected_prefixes),
            "content_change_allowlist": sorted(policy.content_change_allowlist),
            "immutable_policy_paths": sorted(policy.immutable_policy_paths),
        },
        "trusted_manifest": [
            _entry_payload(expected_protected[path])
            for path in sorted(expected_protected)
        ],
        "candidate_protected": [
            _entry_payload(candidate_protected[path])
            for path in sorted(candidate_protected)
        ],
        "allowed_content_changes": sorted(allowed_changes),
    }
    evidence_sha256 = hashlib.sha256(
        json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    return ProtectedTreeGateResult(
        status="PASS",
        protected_entry_count=len(expected_protected),
        allowed_content_changes=tuple(sorted(allowed_changes)),
        evidence_sha256=evidence_sha256,
        candidate_ci_eligible=True,
    )


def _entry_payload(entry: GitTreeEntry) -> dict[str, str]:
    return {
        "path": entry.path,
        "mode": entry.mode,
        "object_type": entry.object_type,
        "object_id": entry.object_id,
    }


def _index_tree(
    entries: Iterable[GitTreeEntry],
    *,
    label: str,
) -> dict[str, GitTreeEntry]:
    result: dict[str, GitTreeEntry] = {}
    aliases: dict[str, str] = {}
    for entry in entries:
        if type(entry) is not GitTreeEntry:
            raise ProtectedTreeGateError(
                f"{label} contains non-GitTreeEntry value"
            )
        path = _canonical_path(entry.path)
        if path in result:
            raise ProtectedTreeGateError(f"{label} contains duplicate path: {path}")
        alias = _windows_alias_key(path)
        previous = aliases.get(alias)
        if previous is not None:
            raise ProtectedTreeGateError(
                f"{label} contains path alias collision: {previous} vs {path}"
            )
        aliases[alias] = path
        result[path] = entry
    return result


def _canonical_prefix(prefix: object) -> str:
    if type(prefix) is not str or not prefix.endswith("/"):
        raise ProtectedTreeGateError(
            "protected_prefixes entries must be canonical paths ending with '/'"
        )
    leaf = prefix[:-1]
    _canonical_path(leaf)
    return prefix


def _canonical_path(path: object) -> str:
    if type(path) is not str or not path:
        raise ProtectedTreeGateError("Git path must be a non-empty string")
    if path != unicodedata.normalize("NFC", path):
        raise ProtectedTreeGateError("Git path must use Unicode NFC")
    if "\\" in path or "\x00" in path:
        raise ProtectedTreeGateError("Git path contains forbidden separator/NUL")
    if path.startswith("/") or path.endswith("/"):
        raise ProtectedTreeGateError("Git path must be relative leaf path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ProtectedTreeGateError("Git path contains empty/traversal segment")
    for part in parts:
        if part.endswith((" ", ".")):
            raise ProtectedTreeGateError(
                "Git path contains Win32 trailing dot/space alias"
            )
    return path


def _windows_alias_key(path: str) -> str:
    return "/".join(
        unicodedata.normalize("NFC", part).casefold()
        for part in path.split("/")
    )
