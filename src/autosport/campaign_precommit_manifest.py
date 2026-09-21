from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .json_integrity import strict_json_loads


SCHEMA = "autosport.campaign_precommit_manifest"
SCHEMA_VERSION = 1
_HEX = frozenset("0123456789abcdef")
_RECORD_FIELDS = {
    "schema",
    "schema_version",
    "campaign_id",
    "source_id",
    "source_snapshot_sha256",
    "committed_at",
    "observation_not_before",
    "observation_not_after",
    "evaluation_universe_sha256",
    "strategy_version_id",
    "champion_version_id",
    "baseline_version_id",
    "cost_contract_sha256",
    "multiplicity_policy_sha256",
    "stopping_policy_sha256",
    "restart_policy_sha256",
    "causal_evidence_policy_sha256",
    "config_sha256",
    "manifest_sha256",
}


class CampaignPrecommitManifestError(ValueError):
    """Invalid, conflicting, or tampered campaign precommit evidence."""


def _text(value: object, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise CampaignPrecommitManifestError(
            f"{field_name} must be non-empty canonical text"
        )
    value.encode("utf-8")
    return value


def _sha(value: object, field_name: str) -> str:
    raw = _text(value, field_name)
    if len(raw) != 64 or raw != raw.lower() or any(ch not in _HEX for ch in raw):
        raise CampaignPrecommitManifestError(
            f"{field_name} must be lowercase SHA-256 hex"
        )
    return raw


def _utc_timestamp(value: object, field_name: str) -> str:
    raw = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignPrecommitManifestError(
            f"{field_name} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignPrecommitManifestError(
            f"{field_name} must include a timezone"
        )
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if raw != canonical:
        raise CampaignPrecommitManifestError(
            f"{field_name} must use canonical UTC Z form"
        )
    return canonical


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _open_bound_posix_parent_directory(path: Path) -> tuple[int, Path]:
    """Open one symlink-free parent lineage and bind later publication to its fd."""

    if os.name == "nt":
        raise CampaignPrecommitManifestError(
            "POSIX campaign precommit parent binding is unavailable on Windows"
        )
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
    ):
        raise CampaignPrecommitManifestError(
            "platform lacks descriptor-relative campaign precommit primitives"
        )

    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(absolute.anchor, flags)
        try:
            for component in absolute.parts[1:]:
                next_descriptor = os.open(
                    component,
                    flags,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
        except BaseException:
            os.close(descriptor)
            raise
    except OSError as exc:
        raise CampaignPrecommitManifestError(
            "campaign precommit parent directory must already exist "
            "without symlink redirection"
        ) from exc
    return descriptor, absolute


def _assert_bound_posix_parent_identity(path: Path, descriptor: int) -> None:
    """Fail closed if the requested parent path stopped naming the bound directory."""

    try:
        current = os.stat(path, follow_symlinks=False)
        bound = os.fstat(descriptor)
    except OSError as exc:
        raise CampaignPrecommitManifestError(
            "cannot revalidate campaign precommit parent identity"
        ) from exc
    if (current.st_dev, current.st_ino) != (bound.st_dev, bound.st_ino):
        raise CampaignPrecommitManifestError(
            "campaign precommit parent identity changed during publication"
        )


def _read_bound_posix_file_bytes(directory_fd: int, name: str) -> bytes:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise CampaignPrecommitManifestError(
            "cannot verify existing campaign precommit manifest"
        ) from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            return handle.read()
    except OSError as exc:
        raise CampaignPrecommitManifestError(
            "cannot verify existing campaign precommit manifest"
        ) from exc


def _fsync_bound_parent_directory(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        raise CampaignPrecommitManifestError(
            "cannot fsync campaign precommit directory"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class CampaignPrecommitManifest:
    """Immutable prospective PAPER-campaign configuration evidence.

    The manifest freezes identities that may affect evaluation before prospective
    observation starts. It is evidence of precommitment only: it does not grant
    provider-write, execution, settlement, promotion, readiness, or real-money
    authority, and write-once file persistence is not an anti-rollback authority.
    """

    campaign_id: str
    source_id: str
    source_snapshot_sha256: str
    committed_at: str
    observation_not_before: str
    observation_not_after: str
    evaluation_universe_sha256: str
    strategy_version_id: str
    champion_version_id: str
    baseline_version_id: str
    cost_contract_sha256: str
    multiplicity_policy_sha256: str
    stopping_policy_sha256: str
    restart_policy_sha256: str
    causal_evidence_policy_sha256: str
    config_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "campaign_id",
            "source_id",
            "strategy_version_id",
            "champion_version_id",
            "baseline_version_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        for name in (
            "source_snapshot_sha256",
            "evaluation_universe_sha256",
            "cost_contract_sha256",
            "multiplicity_policy_sha256",
            "stopping_policy_sha256",
            "restart_policy_sha256",
            "causal_evidence_policy_sha256",
            "config_sha256",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        for name in (
            "committed_at",
            "observation_not_before",
            "observation_not_after",
        ):
            object.__setattr__(
                self,
                name,
                _utc_timestamp(getattr(self, name), name),
            )

        committed = _instant(self.committed_at)
        first_observation = _instant(self.observation_not_before)
        last_observation = _instant(self.observation_not_after)
        if committed >= first_observation:
            raise CampaignPrecommitManifestError(
                "precommit must be committed before prospective observation begins"
            )
        if first_observation >= last_observation:
            raise CampaignPrecommitManifestError(
                "observation_not_before must precede observation_not_after"
            )
        if self.champion_version_id == self.baseline_version_id:
            raise CampaignPrecommitManifestError(
                "champion_version_id and baseline_version_id must be distinct"
            )

    def payload(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "campaign_id": self.campaign_id,
            "source_id": self.source_id,
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "committed_at": self.committed_at,
            "observation_not_before": self.observation_not_before,
            "observation_not_after": self.observation_not_after,
            "evaluation_universe_sha256": self.evaluation_universe_sha256,
            "strategy_version_id": self.strategy_version_id,
            "champion_version_id": self.champion_version_id,
            "baseline_version_id": self.baseline_version_id,
            "cost_contract_sha256": self.cost_contract_sha256,
            "multiplicity_policy_sha256": self.multiplicity_policy_sha256,
            "stopping_policy_sha256": self.stopping_policy_sha256,
            "restart_policy_sha256": self.restart_policy_sha256,
            "causal_evidence_policy_sha256": self.causal_evidence_policy_sha256,
            "config_sha256": self.config_sha256,
        }

    @property
    def manifest_sha256(self) -> str:
        return _digest(self.payload())

    def to_record(self) -> dict[str, object]:
        return {
            **self.payload(),
            "manifest_sha256": self.manifest_sha256,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "CampaignPrecommitManifest":
        if type(record) is not dict:
            raise CampaignPrecommitManifestError(
                "precommit record must be an exact JSON object"
            )
        if set(record) != _RECORD_FIELDS:
            raise CampaignPrecommitManifestError(
                "precommit record fields do not match schema"
            )
        if record.get("schema") != SCHEMA:
            raise CampaignPrecommitManifestError("precommit schema mismatch")
        version = record.get("schema_version")
        if type(version) is not int or version != SCHEMA_VERSION:
            raise CampaignPrecommitManifestError("precommit schema version mismatch")

        manifest = cls(
            campaign_id=record["campaign_id"],
            source_id=record["source_id"],
            source_snapshot_sha256=record["source_snapshot_sha256"],
            committed_at=record["committed_at"],
            observation_not_before=record["observation_not_before"],
            observation_not_after=record["observation_not_after"],
            evaluation_universe_sha256=record["evaluation_universe_sha256"],
            strategy_version_id=record["strategy_version_id"],
            champion_version_id=record["champion_version_id"],
            baseline_version_id=record["baseline_version_id"],
            cost_contract_sha256=record["cost_contract_sha256"],
            multiplicity_policy_sha256=record["multiplicity_policy_sha256"],
            stopping_policy_sha256=record["stopping_policy_sha256"],
            restart_policy_sha256=record["restart_policy_sha256"],
            causal_evidence_policy_sha256=record["causal_evidence_policy_sha256"],
            config_sha256=record["config_sha256"],
        )
        claimed = _sha(record["manifest_sha256"], "manifest_sha256")
        if claimed != manifest.manifest_sha256:
            raise CampaignPrecommitManifestError(
                "precommit manifest digest mismatch"
            )
        return manifest


def load_campaign_precommit_manifest(
    path: str | os.PathLike[str],
) -> CampaignPrecommitManifest:
    target = Path(path)
    if os.name == "nt":
        try:
            raw_bytes = target.read_bytes()
        except OSError as exc:
            raise CampaignPrecommitManifestError(
                "cannot read campaign precommit manifest"
            ) from exc
    else:
        name = target.name
        if name in {"", ".", ".."} or Path(name).name != name:
            raise CampaignPrecommitManifestError(
                "campaign precommit target must name one file"
            )
        parent_fd, absolute_parent = _open_bound_posix_parent_directory(target.parent)
        try:
            raw_bytes = _read_bound_posix_file_bytes(parent_fd, name)
            _assert_bound_posix_parent_identity(absolute_parent, parent_fd)
        finally:
            os.close(parent_fd)

    try:
        text = raw_bytes.decode("utf-8", errors="strict")
        raw = strict_json_loads(text)
    except (UnicodeError, ValueError) as exc:
        raise CampaignPrecommitManifestError(
            "cannot read campaign precommit manifest"
        ) from exc

    manifest = CampaignPrecommitManifest.from_record(raw)
    canonical_bytes = _canonical_bytes(manifest.to_record()) + b"\n"
    if raw_bytes != canonical_bytes:
        raise CampaignPrecommitManifestError(
            "campaign precommit manifest bytes are not canonical"
        )
    return manifest


def write_campaign_precommit_manifest_once(
    path: str | os.PathLike[str],
    manifest: CampaignPrecommitManifest,
) -> str:
    """Persist exact precommit bytes once, with byte-identical idempotent retry.

    This protects the local artifact against silent in-place replacement and makes
    later byte tampering detectable. The parent directory must already exist and be
    provisioned by the canonical workspace/storage authority: this writer will not
    silently create a directory lineage whose crash durability it cannot prove. On
    POSIX, publication is descriptor-relative to one symlink-free parent identity.
    It intentionally does not claim rollback protection if an attacker can delete
    and recreate the whole workspace after publication.
    """

    if type(manifest) is not CampaignPrecommitManifest:
        raise CampaignPrecommitManifestError(
            "manifest must be CampaignPrecommitManifest"
        )
    target = Path(path)
    encoded = _canonical_bytes(manifest.to_record()) + b"\n"

    if os.name == "nt":
        if not target.parent.is_dir():
            raise CampaignPrecommitManifestError(
                "campaign precommit parent directory must already exist"
            )
        try:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            try:
                existing = target.read_bytes()
            except OSError as exc:
                raise CampaignPrecommitManifestError(
                    "cannot verify existing campaign precommit manifest"
                ) from exc
            if existing != encoded:
                raise CampaignPrecommitManifestError(
                    "existing campaign precommit manifest conflicts with precommit"
                )
        except OSError as exc:
            raise CampaignPrecommitManifestError(
                "cannot create campaign precommit manifest"
            ) from exc
        else:
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    target.unlink()
                except OSError:
                    pass
                raise

        verified = load_campaign_precommit_manifest(target)
        if verified.manifest_sha256 != manifest.manifest_sha256:
            raise CampaignPrecommitManifestError(
                "persisted campaign precommit manifest failed exact re-read"
            )
        return manifest.manifest_sha256

    name = target.name
    if name in {"", ".", ".."} or Path(name).name != name:
        raise CampaignPrecommitManifestError(
            "campaign precommit target must name one file"
        )

    parent_fd, absolute_parent = _open_bound_posix_parent_directory(target.parent)
    try:
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            existing = _read_bound_posix_file_bytes(parent_fd, name)
            if existing != encoded:
                raise CampaignPrecommitManifestError(
                    "existing campaign precommit manifest conflicts with precommit"
                )
        except OSError as exc:
            raise CampaignPrecommitManifestError(
                "cannot create campaign precommit manifest"
            ) from exc
        else:
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                try:
                    os.unlink(name, dir_fd=parent_fd)
                except OSError:
                    pass
                raise

        _fsync_bound_parent_directory(parent_fd)
        _assert_bound_posix_parent_identity(absolute_parent, parent_fd)
        if _read_bound_posix_file_bytes(parent_fd, name) != encoded:
            raise CampaignPrecommitManifestError(
                "persisted campaign precommit manifest failed exact re-read"
            )
        return manifest.manifest_sha256
    finally:
        os.close(parent_fd)
