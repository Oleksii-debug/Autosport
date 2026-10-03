from __future__ import annotations

"""Durable pre-observation campaign inception over existing product authorities.

This module creates no second scheduler, universe store, observation ledger, or money
authority. Before any collector START it composes:

1. the exact #1257 CampaignPrecommitPublicationWitness + manifest;
2. one exact #1180 scheduled source/run prepared with the durable START gate; and
3. the integrated MonotonicWorkspaceAuthority as independent rollback fencing.

Only after the inception state is durably COMMITTED in the monotonic authority is the
exact schedule gate authorized. The later #1185 provider-universe resolver remains a
post-observation validation step: this receipt binds the prospectively selected
evaluation_universe_sha256 from #1257, never fabricates a post-observation universe
receipt before its rows exist.
"""

import hashlib
import inspect
import json
import math
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Mapping

from .campaign_precommit_manifest import (
    CampaignPrecommitManifest,
    CampaignPrecommitManifestError,
    CampaignPrecommitPublicationWitness,
    load_campaign_precommit_manifest,
    resolve_campaign_precommit_publication_witness,
)
from .causal_collector import CollectorDeltaStore
from .forward_universe_precommit_authority import ForwardUniversePrecommitLocator
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import (
    AuthorityPhase,
    AuthorityRecord,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .scheduled_source_universe import (
    PreparedScheduledSourceUniverse,
    ScheduledSourceUniverseError,
    prepare_scheduled_source_universe,
)
from .workspace_lock import WorkspaceEconomicLock, _open_read_only_descriptor


SCHEMA = "autosport.campaign_inception_receipt"
SCHEMA_VERSION = 1
AUTHORITY_DOMAIN = "research.forward-campaign-inception-causality"
_STATE_DIR = "campaign-inception-v1"
_HEX = frozenset("0123456789abcdef")
_MAX_STATE_BYTES = 1024 * 1024

_CANONICAL_WITNESS_RESOLVER = resolve_campaign_precommit_publication_witness
_CANONICAL_MANIFEST_LOADER = load_campaign_precommit_manifest
_CANONICAL_PRESTART_PREPARER = prepare_scheduled_source_universe
_CANONICAL_NEXT_SLOT = CollectorDeltaStore._next_collector_schedule_slot
_CANONICAL_GATE_STATUS = CollectorDeltaStore._collector_schedule_start_gate_status
_CANONICAL_GATE_AUTHORIZE = CollectorDeltaStore._authorize_collector_schedule_start_gate
_CANONICAL_SCHEDULE_ID = CollectorDeltaStore._collector_schedule_id
_CANONICAL_SCHEDULE_DUE_AT = CollectorDeltaStore._collector_schedule_due_at
_CANONICAL_PATH_EQUALITY = Path.__eq__
_CANONICAL_PATH_FSPATH = Path.__fspath__
_CANONICAL_OS_FSPATH = os.fspath
_CANONICAL_ABSPATH = os.path.abspath
_CANONICAL_STATE_READ_OPEN = _open_read_only_descriptor
_CANONICAL_STORE_SEAMS = frozenset(
    {
        "_next_collector_schedule_slot",
        "_collector_schedule_start_gate_status",
        "_authorize_collector_schedule_start_gate",
        "_connect",
        "_connect_path",
        "_path_file_identity",
        "_collector_schedule_id",
        "_collector_schedule_due_at",
        "_schedule_max_items",
        "_schedule_authority_sha256",
    }
)
_CANONICAL_STORE_CLASS_SEAMS = {
    name: inspect.getattr_static(CollectorDeltaStore, name)
    for name in _CANONICAL_STORE_SEAMS
}


class CampaignInceptionError(RuntimeError):
    """Campaign inception cannot be established from exact prospective authority."""


class CampaignInceptionConflictError(CampaignInceptionError):
    """Existing campaign/gate state conflicts with the requested inception identity."""


class CampaignInceptionIntegrityError(CampaignInceptionError):
    """Persisted campaign inception evidence is malformed, rolled back, or corrupt."""


def _text(value: object, name: str, *, max_length: int = 1024) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise CampaignInceptionIntegrityError(
            f"{name} must be non-empty canonical text"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CampaignInceptionIntegrityError(
            f"{name} contains invalid Unicode"
        ) from exc
    return value


def _sha256(value: object, name: str) -> str:
    raw = _text(value, name, max_length=64)
    if (
        len(raw) != 64
        or raw != raw.lower()
        or any(character not in _HEX for character in raw)
    ):
        raise CampaignInceptionIntegrityError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return raw


def _instant(value: object, name: str) -> datetime:
    raw = _text(value, name, max_length=128)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignInceptionIntegrityError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignInceptionIntegrityError(f"{name} must include timezone")
    return parsed.astimezone(UTC)


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CampaignInceptionIntegrityError(
            "campaign inception state is outside canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _campaign_key(campaign_id: str) -> str:
    return hashlib.sha256(
        ("campaign-inception-v1\x00" + campaign_id).encode("utf-8")
    ).hexdigest()


def _state_path(workspace: Path, campaign_id: str) -> Path:
    return workspace / _STATE_DIR / f"{_campaign_key(campaign_id)}.json"


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_state(path: Path, payload: Mapping[str, object]) -> None:
    """Atomically publish one local receipt image under the campaign writer lock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _canonical_bytes(dict(payload)) + b"\n"
    if len(encoded) > _MAX_STATE_BYTES:
        raise CampaignInceptionIntegrityError(
            "campaign inception state exceeds supported size"
        )
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(temporary, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        created = False
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise


def _read_stable_state_bytes(path: Path) -> bytes | None:
    """Read one bounded regular inception-state file from one stable identity."""

    try:
        before = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CampaignInceptionIntegrityError(
            "cannot inspect campaign inception state"
        ) from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise CampaignInceptionIntegrityError(
            "campaign inception state must be one regular file"
        )
    if before.st_size > _MAX_STATE_BYTES:
        raise CampaignInceptionIntegrityError(
            "campaign inception state exceeds supported size"
        )

    descriptor: int | None = None
    primary_error: BaseException | None = None
    try:
        descriptor = _CANONICAL_STATE_READ_OPEN(path)
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            raise CampaignInceptionIntegrityError(
                "campaign inception state changed during open"
            )

        chunks: list[bytes] = []
        remaining = _MAX_STATE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after_open = os.fstat(descriptor)
        after = os.stat(path, follow_symlinks=False)
    except CampaignInceptionIntegrityError as exc:
        primary_error = exc
        raise
    except OSError as exc:
        primary_error = exc
        raise CampaignInceptionIntegrityError(
            "cannot read campaign inception state"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as close_error:
                if primary_error is None:
                    raise CampaignInceptionIntegrityError(
                        "campaign inception state descriptor cleanup failed"
                    ) from close_error
                try:
                    primary_error.add_note(
                        "campaign inception state descriptor cleanup also failed"
                    )
                except BaseException:
                    pass

    if len(payload) > _MAX_STATE_BYTES:
        raise CampaignInceptionIntegrityError(
            "campaign inception state exceeds supported size"
        )

    def identity(
        value: os.stat_result,
    ) -> tuple[int, int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            value.st_nlink,
        )

    if identity(before) != identity(after) or identity(opened) != identity(after_open):
        raise CampaignInceptionIntegrityError(
            "campaign inception state changed during stable read"
        )
    return payload


def _read_state(path: Path) -> dict[str, object] | None:
    raw = _read_stable_state_bytes(path)
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8", errors="strict")
        payload = strict_json_loads(text)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise CampaignInceptionIntegrityError(
            "campaign inception state is invalid JSON"
        ) from exc
    if type(payload) is not dict:
        raise CampaignInceptionIntegrityError(
            "campaign inception state must be an exact JSON object"
        )
    if raw != _canonical_bytes(payload) + b"\n":
        raise CampaignInceptionIntegrityError(
            "campaign inception state bytes are not canonical"
        )
    return payload


@dataclass(frozen=True, slots=True)
class CampaignInceptionSourceSpec:
    """Caller routing only; positive authority is re-resolved from product stores."""

    expected_store_path: Path
    source_id: str
    run_id: str
    stream_epoch: str
    anchor_at: str
    interval_seconds: float
    max_items: int
    evaluation_start_slot_ordinal: int
    evaluation_end_slot_ordinal: int

    def __post_init__(self) -> None:
        # A Path subclass can override is_absolute()/__fspath__() and execute
        # caller code while the exact collector-store authority path is still being
        # constructed.  This DTO is authority-bearing input, so admit only the exact
        # platform pathlib concrete type before any virtual path dispatch.
        if type(self.expected_store_path) is not type(Path()):
            raise TypeError(
                "expected_store_path must be the exact platform pathlib path type"
            )
        if not self.expected_store_path.is_absolute():
            raise CampaignInceptionIntegrityError(
                "expected_store_path must be absolute"
            )
        object.__setattr__(
            self,
            "expected_store_path",
            Path(os.path.abspath(self.expected_store_path)),
        )
        for field_name in ("source_id", "run_id", "stream_epoch"):
            object.__setattr__(
                self,
                field_name,
                _text(getattr(self, field_name), field_name, max_length=512),
            )
        canonical_anchor = _instant(self.anchor_at, "anchor_at").isoformat()
        object.__setattr__(self, "anchor_at", canonical_anchor)
        # This value feeds the durable schedule/gate identity before the sealed
        # campaign-establishment entrypoint is reached.  Do not invoke caller-defined
        # numeric protocols (for example a float subclass overriding __float__) while
        # constructing authority-bearing schedule input.
        if type(self.interval_seconds) not in {int, float}:
            raise CampaignInceptionIntegrityError(
                "interval_seconds must be an exact built-in int or float"
            )
        interval_seconds = float(self.interval_seconds)
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise CampaignInceptionIntegrityError(
                "interval_seconds must be positive and finite"
            )
        object.__setattr__(self, "interval_seconds", interval_seconds)
        if type(self.max_items) is not int or self.max_items <= 0:
            raise CampaignInceptionIntegrityError(
                "max_items must be a positive integer"
            )
        if (
            type(self.evaluation_start_slot_ordinal) is not int
            or self.evaluation_start_slot_ordinal != 0
        ):
            raise CampaignInceptionIntegrityError(
                "campaign inception requires exact integer slot zero as first slot"
            )
        if (
            type(self.evaluation_end_slot_ordinal) is not int
            or self.evaluation_end_slot_ordinal < 0
        ):
            raise CampaignInceptionIntegrityError(
                "evaluation_end_slot_ordinal must be a non-negative integer"
            )

    def payload(self) -> dict[str, object]:
        return {
            "expected_store_path": str(self.expected_store_path),
            "source_id": self.source_id,
            "run_id": self.run_id,
            "stream_epoch": self.stream_epoch,
            "anchor_at": self.anchor_at,
            "interval_seconds": repr(self.interval_seconds),
            "max_items": self.max_items,
            "evaluation_start_slot_ordinal": self.evaluation_start_slot_ordinal,
            "evaluation_end_slot_ordinal": self.evaluation_end_slot_ordinal,
        }


@dataclass(frozen=True, slots=True, init=False)
class CampaignInceptionReceipt:
    """Resolver-issued receipt proving durable inception before gated START."""

    schema_version: int
    campaign_id: str
    manifest_sha256: str
    evaluation_universe_sha256: str
    source_snapshot_sha256: str
    source_id: str
    publication_authority_id: str
    publication_authority_generation: int
    publication_authority_record_sha256: str
    publication_semantic_binding_sha256: str
    workspace_instance_id: str
    post_publish_observed_at: str
    expected_store_path: str
    run_id: str
    stream_epoch: str
    schedule_id: str
    gate_binding_sha256: str
    prestart_sha256: str
    authority_generation: int
    authority_record_sha256: str
    semantic_binding_sha256: str
    receipt_sha256: str

    def __new__(cls, *args: object, **kwargs: object) -> "CampaignInceptionReceipt":
        raise TypeError(
            "CampaignInceptionReceipt is resolver-issued; "
            "call establish_campaign_inception"
        )

    @classmethod
    def _issue(cls, values: Mapping[str, object]) -> "CampaignInceptionReceipt":
        instance = object.__new__(cls)
        for name in cls.__dataclass_fields__:
            object.__setattr__(instance, name, values[name])
        return instance

    @property
    def start_authorization_sha256(self) -> str:
        return self.authority_record_sha256

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def _require_store_seams(store: CollectorDeltaStore) -> None:
    if type(store) is not CollectorDeltaStore:
        raise TypeError("store must be the exact canonical CollectorDeltaStore")
    _require_store_seams_class_only()
    instance_state = vars(store)
    rebound = sorted(name for name in _CANONICAL_STORE_SEAMS if name in instance_state)
    if rebound:
        raise CampaignInceptionIntegrityError(
            "collector store campaign seam is instance-rebound: " + ", ".join(rebound)
        )


def _resolve_precommit(
    locator: ForwardUniversePrecommitLocator,
) -> tuple[CampaignPrecommitManifest, CampaignPrecommitPublicationWitness]:
    if type(locator) is not ForwardUniversePrecommitLocator:
        raise TypeError("precommit locator must be exact ForwardUniversePrecommitLocator")
    resolver_code = _CANONICAL_WITNESS_RESOLVER.__code__
    loader_code = _CANONICAL_MANIFEST_LOADER.__code__
    try:
        witness = _CANONICAL_WITNESS_RESOLVER(
            locator.absolute_manifest_path,
            workspace=locator.workspace,
            workspace_instance_id=locator.workspace_instance_id,
            authority_root=locator.authority_root,
        )
        manifest = _CANONICAL_MANIFEST_LOADER(locator.absolute_manifest_path)
    except CampaignPrecommitManifestError as exc:
        raise CampaignInceptionIntegrityError(
            "campaign precommit authority cannot be resolved"
        ) from exc
    if (
        _CANONICAL_WITNESS_RESOLVER.__code__ is not resolver_code
        or _CANONICAL_MANIFEST_LOADER.__code__ is not loader_code
    ):
        raise CampaignInceptionIntegrityError(
            "campaign precommit executable changed during inception resolution"
        )
    if type(manifest) is not CampaignPrecommitManifest:
        raise CampaignInceptionIntegrityError("precommit manifest type is noncanonical")
    if type(witness) is not CampaignPrecommitPublicationWitness:
        raise CampaignInceptionIntegrityError(
            "precommit publication witness type is noncanonical"
        )
    if (
        witness.campaign_id != manifest.campaign_id
        or witness.manifest_sha256 != manifest.manifest_sha256
    ):
        raise CampaignInceptionIntegrityError(
            "publication witness does not bind current precommit manifest"
        )
    if (
        locator.workspace_instance_id is not None
        and witness.workspace_instance_id != locator.workspace_instance_id
    ):
        raise CampaignInceptionIntegrityError(
            "publication witness workspace identity changed"
        )
    return manifest, witness


def _precommit_payload(
    manifest: CampaignPrecommitManifest,
    witness: CampaignPrecommitPublicationWitness,
) -> dict[str, object]:
    return {
        "campaign_id": manifest.campaign_id,
        "manifest_sha256": manifest.manifest_sha256,
        "evaluation_universe_sha256": manifest.evaluation_universe_sha256,
        "source_snapshot_sha256": manifest.source_snapshot_sha256,
        "source_id": manifest.source_id,
        "config_sha256": manifest.config_sha256,
        "causal_evidence_policy_sha256": manifest.causal_evidence_policy_sha256,
        "observation_not_before": manifest.observation_not_before,
        "observation_not_after": manifest.observation_not_after,
        "publication_authority_id": witness.authority_id,
        "publication_authority_generation": witness.authority_generation,
        "publication_authority_record_sha256": witness.authority_record_sha256,
        "publication_semantic_binding_sha256": witness.semantic_binding_sha256,
        "workspace_instance_id": witness.workspace_instance_id,
        "post_publish_observed_at": witness.post_publish_observed_at,
        "target_relative_path": witness.target_relative_path,
    }


def _validate_schedule_window(
    manifest: CampaignPrecommitManifest,
    spec: CampaignInceptionSourceSpec,
) -> None:
    if spec.source_id != manifest.source_id:
        raise CampaignInceptionConflictError(
            "campaign source does not match prospective precommit source"
        )
    anchor = _instant(spec.anchor_at, "anchor_at")
    not_before = _instant(manifest.observation_not_before, "observation_not_before")
    not_after = _instant(manifest.observation_not_after, "observation_not_after")
    last_due = anchor + timedelta(
        seconds=spec.interval_seconds * spec.evaluation_end_slot_ordinal
    )
    if anchor < not_before or last_due > not_after:
        raise CampaignInceptionConflictError(
            "collector evaluation schedule falls outside precommitted observation window"
        )


def _gate_binding_sha256(
    *,
    precommit: Mapping[str, object],
    spec: CampaignInceptionSourceSpec,
) -> str:
    return _digest(
        {
            "domain": "autosport.campaign-inception-start-gate.v1",
            "precommit": dict(precommit),
            "source_spec": spec.payload(),
        }
    )


def _semantic_binding_sha256(
    *,
    precommit: Mapping[str, object],
    spec: CampaignInceptionSourceSpec,
    prepared: Mapping[str, object],
) -> str:
    return _digest(
        {
            "domain": "autosport.campaign-inception-receipt-binding.v1",
            "precommit": dict(precommit),
            "source_spec": spec.payload(),
            "prepared_schedule": dict(prepared),
            "admission_rule": (
                "exact campaign receipt COMMIT before exact gated scheduled START; "
                "post-observation universe must re-resolve to prospectively frozen digest"
            ),
        }
    )


def _new_state_payload(
    *,
    precommit: Mapping[str, object],
    spec: CampaignInceptionSourceSpec,
    prepared: PreparedScheduledSourceUniverse,
) -> dict[str, object]:
    prepared_payload = prepared.to_dict()
    semantic = _semantic_binding_sha256(
        precommit=precommit,
        spec=spec,
        prepared=prepared_payload,
    )
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "tx_id": f"campaign-inception:{uuid.uuid4().hex}",
        "semantic_binding_sha256": semantic,
        "precommit": dict(precommit),
        "source_spec": spec.payload(),
        "prepared_schedule": prepared_payload,
    }


def _validate_state(
    payload: Mapping[str, object],
    *,
    precommit: Mapping[str, object],
    spec: CampaignInceptionSourceSpec,
) -> tuple[str, str, dict[str, object]]:
    expected = {
        "schema",
        "schema_version",
        "tx_id",
        "semantic_binding_sha256",
        "precommit",
        "source_spec",
        "prepared_schedule",
    }
    if (
        set(payload) != expected
        or payload.get("schema") != SCHEMA
        or payload.get("schema_version") != SCHEMA_VERSION
    ):
        raise CampaignInceptionIntegrityError(
            "campaign inception state schema is noncanonical"
        )
    if payload.get("precommit") != dict(precommit):
        raise CampaignInceptionConflictError(
            "campaign identity is already bound to different precommit authority"
        )
    if payload.get("source_spec") != spec.payload():
        raise CampaignInceptionConflictError(
            "campaign identity is already bound to a different collector source/run"
        )
    prepared = payload.get("prepared_schedule")
    if type(prepared) is not dict:
        raise CampaignInceptionIntegrityError(
            "prepared schedule payload must be an exact object"
        )
    expected_prepared_fields = {
        "schema_version",
        "source_id",
        "run_id",
        "stream_epoch",
        "schedule_id",
        "schedule_policy",
        "anchor_at",
        "interval_seconds",
        "max_items",
        "evaluation_start_slot_ordinal",
        "evaluation_end_slot_ordinal",
        "next_slot_ordinal",
        "next_due_at",
        "gate_binding_sha256",
        "prestart_sha256",
    }
    if set(prepared) != expected_prepared_fields:
        raise CampaignInceptionIntegrityError(
            "prepared schedule payload schema is noncanonical"
        )
    if (
        prepared.get("source_id") != spec.source_id
        or prepared.get("run_id") != spec.run_id
        or prepared.get("stream_epoch") != spec.stream_epoch
        or prepared.get("schedule_policy") != "fixed_interval_v1"
        or prepared.get("evaluation_start_slot_ordinal")
        != spec.evaluation_start_slot_ordinal
        or prepared.get("evaluation_end_slot_ordinal")
        != spec.evaluation_end_slot_ordinal
        or prepared.get("next_slot_ordinal") != 0
        or prepared.get("gate_binding_sha256")
        != _gate_binding_sha256(precommit=precommit, spec=spec)
    ):
        raise CampaignInceptionIntegrityError(
            "prepared schedule does not match exact inception source specification"
        )
    _sha256(prepared.get("schedule_id"), "schedule_id")
    _sha256(prepared.get("prestart_sha256"), "prestart_sha256")
    tx_id = _text(payload.get("tx_id"), "tx_id", max_length=256)
    semantic = _sha256(
        payload.get("semantic_binding_sha256"),
        "semantic_binding_sha256",
    )
    expected_semantic = _semantic_binding_sha256(
        precommit=precommit,
        spec=spec,
        prepared=prepared,
    )
    if semantic != expected_semantic:
        raise CampaignInceptionIntegrityError(
            "campaign inception semantic binding digest mismatch"
        )
    return tx_id, semantic, prepared


def _authority(
    *,
    locator: ForwardUniversePrecommitLocator,
    witness: CampaignPrecommitPublicationWitness,
    campaign_id: str,
) -> MonotonicWorkspaceAuthority:
    try:
        return MonotonicWorkspaceAuthority(
            workspace=locator.workspace,
            workspace_instance_id=witness.workspace_instance_id,
            domain=AUTHORITY_DOMAIN,
            key=campaign_id,
            authority_root=locator.authority_root,
        )
    except MonotonicWorkspaceAuthorityError as exc:
        raise CampaignInceptionIntegrityError(
            "cannot resolve independent campaign inception authority"
        ) from exc


def _record_for_recovery(recovery: object) -> AuthorityRecord:
    record = getattr(recovery, "record", None)
    if type(record) is not AuthorityRecord:
        raise CampaignInceptionIntegrityError(
            "campaign inception authority has no committed record"
        )
    return record


def _expected_schedule_id(spec: CampaignInceptionSourceSpec) -> str:
    _require_store_seams_class_only()
    try:
        result = _CANONICAL_SCHEDULE_ID(
            source_id=spec.source_id,
            run_id=spec.run_id,
            stream_epoch=spec.stream_epoch,
            anchor_at=spec.anchor_at,
            interval_seconds=repr(spec.interval_seconds),
            max_items=spec.max_items,
            evaluation_start_slot_ordinal=spec.evaluation_start_slot_ordinal,
            evaluation_end_slot_ordinal=spec.evaluation_end_slot_ordinal,
        )
    except (TypeError, ValueError) as exc:
        raise CampaignInceptionIntegrityError(
            "cannot compute canonical collector schedule identity"
        ) from exc
    return _sha256(result, "expected schedule_id")


def _canonical_absolute_path_text(value: object) -> str:
    """Resolve a Path through import-time captured path/filesystem dispatch only."""

    if not isinstance(value, Path):
        raise CampaignInceptionIntegrityError(
            "campaign inception path identity is unavailable"
        )
    try:
        raw = _CANONICAL_PATH_FSPATH(value)
        canonical = _CANONICAL_ABSPATH(raw)
    except (OSError, TypeError, ValueError) as exc:
        raise CampaignInceptionIntegrityError(
            "campaign inception path identity cannot be canonicalized"
        ) from exc
    if type(raw) is not str or type(canonical) is not str:
        raise CampaignInceptionIntegrityError(
            "campaign inception path identity is noncanonical"
        )
    return canonical


def _prepared_payload_from_existing_gate(
    *,
    store: CollectorDeltaStore,
    spec: CampaignInceptionSourceSpec,
    gate_binding_sha256: str,
) -> dict[str, object]:
    """Reconstruct the original slot-zero preparation without creating authority."""

    _require_store_seams(store)
    if (
        _canonical_absolute_path_text(store.path)
        != _canonical_absolute_path_text(spec.expected_store_path)
    ):
        raise CampaignInceptionIntegrityError(
            "collector store path changed from campaign receipt"
        )
    try:
        slot = _CANONICAL_NEXT_SLOT(
            store,
            source_id=spec.source_id,
            run_id=spec.run_id,
        )
        gate = _CANONICAL_GATE_STATUS(
            store,
            source_id=spec.source_id,
            run_id=spec.run_id,
        )
        due_zero = _CANONICAL_SCHEDULE_DUE_AT(
            anchor_at=spec.anchor_at,
            interval_seconds=repr(spec.interval_seconds),
            slot_ordinal=0,
        )
    except (TypeError, ValueError) as exc:
        raise CampaignInceptionIntegrityError(
            "committed campaign schedule/gate cannot be re-resolved"
        ) from exc
    _require_store_seams(store)
    if type(slot) is not dict or type(gate) is not dict:
        raise CampaignInceptionIntegrityError(
            "committed campaign schedule/gate evidence is missing"
        )
    expected_id = _expected_schedule_id(spec)
    if (
        slot.get("schedule_id") != expected_id
        or slot.get("stream_epoch") != spec.stream_epoch
        or slot.get("max_items") != spec.max_items
        or gate.get("schedule_id") != expected_id
        or gate.get("gate_binding_sha256") != gate_binding_sha256
    ):
        raise CampaignInceptionIntegrityError(
            "committed campaign schedule/gate identity changed"
        )
    payload: dict[str, object] = {
        "schema_version": 1,
        "source_id": spec.source_id,
        "run_id": spec.run_id,
        "stream_epoch": spec.stream_epoch,
        "schedule_id": expected_id,
        "schedule_policy": "fixed_interval_v1",
        "anchor_at": spec.anchor_at,
        "interval_seconds": repr(spec.interval_seconds),
        "max_items": spec.max_items,
        "evaluation_start_slot_ordinal": spec.evaluation_start_slot_ordinal,
        "evaluation_end_slot_ordinal": spec.evaluation_end_slot_ordinal,
        "next_slot_ordinal": 0,
        "next_due_at": due_zero,
        "gate_binding_sha256": gate_binding_sha256,
    }
    payload["prestart_sha256"] = _digest(payload)
    return payload


def _require_store_seams_class_only() -> None:
    rebound = sorted(
        name
        for name, expected in _CANONICAL_STORE_CLASS_SEAMS.items()
        if inspect.getattr_static(CollectorDeltaStore, name, None) is not expected
    )
    if rebound:
        raise CampaignInceptionIntegrityError(
            "collector store campaign seam is class-rebound: " + ", ".join(rebound)
        )


def _resolve_gate_and_authorize(
    *,
    store: CollectorDeltaStore,
    spec: CampaignInceptionSourceSpec,
    prepared: Mapping[str, object],
    authority_record_sha256: str,
) -> None:
    _require_store_seams(store)
    if (
        _canonical_absolute_path_text(store.path)
        != _canonical_absolute_path_text(spec.expected_store_path)
    ):
        raise CampaignInceptionIntegrityError(
            "collector store path changed from campaign receipt"
        )
    try:
        slot = _CANONICAL_NEXT_SLOT(
            store,
            source_id=spec.source_id,
            run_id=spec.run_id,
        )
        gate = _CANONICAL_GATE_STATUS(
            store,
            source_id=spec.source_id,
            run_id=spec.run_id,
        )
    except (TypeError, ValueError) as exc:
        raise CampaignInceptionIntegrityError(
            "campaign collector schedule/gate cannot be re-resolved"
        ) from exc
    _require_store_seams(store)
    if type(slot) is not dict or type(gate) is not dict:
        raise CampaignInceptionIntegrityError(
            "campaign collector gate evidence is noncanonical"
        )
    schedule_id = _sha256(prepared.get("schedule_id"), "schedule_id")
    gate_binding = _sha256(
        prepared.get("gate_binding_sha256"),
        "gate_binding_sha256",
    )
    expected_schedule_id = _expected_schedule_id(spec)
    if (
        schedule_id != expected_schedule_id
        or slot.get("schedule_id") != schedule_id
        or slot.get("stream_epoch") != spec.stream_epoch
        or slot.get("max_items") != spec.max_items
        or gate.get("schedule_id") != schedule_id
        or gate.get("gate_binding_sha256") != gate_binding
    ):
        raise CampaignInceptionIntegrityError(
            "campaign collector schedule/gate identity changed"
        )
    current_authorization = gate.get("authorization_sha256")
    if current_authorization is None:
        if slot.get("slot_ordinal") != 0:
            raise CampaignInceptionIntegrityError(
                "collector START exists before campaign gate authorization"
            )
        try:
            result = _CANONICAL_GATE_AUTHORIZE(
                store,
                source_id=spec.source_id,
                run_id=spec.run_id,
                schedule_id=schedule_id,
                gate_binding_sha256=gate_binding,
                authorization_sha256=authority_record_sha256,
            )
        except (TypeError, ValueError) as exc:
            raise CampaignInceptionIntegrityError(
                "cannot authorize campaign collector START gate"
            ) from exc
        if (
            type(result) is not dict
            or result.get("schedule_id") != schedule_id
            or result.get("gate_binding_sha256") != gate_binding
            or result.get("authorization_sha256") != authority_record_sha256
        ):
            raise CampaignInceptionIntegrityError(
                "collector START gate authorization result is noncanonical"
            )
    elif current_authorization != authority_record_sha256:
        raise CampaignInceptionIntegrityError(
            "collector START gate is authorized by different campaign authority"
        )
    _require_store_seams(store)


def _issue_receipt(
    *,
    precommit: Mapping[str, object],
    prepared: Mapping[str, object],
    spec: CampaignInceptionSourceSpec,
    semantic_binding_sha256: str,
    state_sha256: str,
    record: AuthorityRecord,
) -> CampaignInceptionReceipt:
    values = {
        "schema_version": SCHEMA_VERSION,
        "campaign_id": precommit["campaign_id"],
        "manifest_sha256": precommit["manifest_sha256"],
        "evaluation_universe_sha256": precommit["evaluation_universe_sha256"],
        "source_snapshot_sha256": precommit["source_snapshot_sha256"],
        "source_id": precommit["source_id"],
        "publication_authority_id": precommit["publication_authority_id"],
        "publication_authority_generation": precommit[
            "publication_authority_generation"
        ],
        "publication_authority_record_sha256": precommit[
            "publication_authority_record_sha256"
        ],
        "publication_semantic_binding_sha256": precommit[
            "publication_semantic_binding_sha256"
        ],
        "workspace_instance_id": precommit["workspace_instance_id"],
        "post_publish_observed_at": precommit["post_publish_observed_at"],
        "expected_store_path": str(spec.expected_store_path),
        "run_id": spec.run_id,
        "stream_epoch": spec.stream_epoch,
        "schedule_id": prepared["schedule_id"],
        "gate_binding_sha256": prepared["gate_binding_sha256"],
        "prestart_sha256": prepared["prestart_sha256"],
        "authority_generation": record.generation,
        "authority_record_sha256": record.record_sha256,
        "semantic_binding_sha256": semantic_binding_sha256,
        "receipt_sha256": state_sha256,
    }
    return CampaignInceptionReceipt._issue(values)


def establish_campaign_inception(
    *,
    precommit_locator: ForwardUniversePrecommitLocator,
    store: CollectorDeltaStore,
    source_spec: CampaignInceptionSourceSpec,
) -> CampaignInceptionReceipt:
    """Commit/reopen one exact campaign receipt, then authorize its START gate."""

    if type(source_spec) is not CampaignInceptionSourceSpec:
        raise TypeError("source_spec must be exact CampaignInceptionSourceSpec")
    _require_store_seams(store)
    manifest, witness = _resolve_precommit(precommit_locator)
    _validate_schedule_window(manifest, source_spec)
    precommit = _precommit_payload(manifest, witness)
    gate_binding = _gate_binding_sha256(precommit=precommit, spec=source_spec)
    state_path = _state_path(precommit_locator.workspace, manifest.campaign_id)
    authority = _authority(
        locator=precommit_locator,
        witness=witness,
        campaign_id=manifest.campaign_id,
    )

    with WorkspaceEconomicLock(state_path.parent):
        existing = _read_state(state_path)
        if existing is not None:
            tx_id, semantic, prepared = _validate_state(
                existing,
                precommit=precommit,
                spec=source_spec,
            )
            state_sha256 = _digest(existing)
            try:
                recovery = authority.recover(
                    observed_state_sha256=state_sha256,
                    tx_id=tx_id,
                    semantic_binding_sha256=semantic,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise CampaignInceptionIntegrityError(
                    "campaign inception receipt is not current in independent authority"
                ) from exc
            record = _record_for_recovery(recovery)
            if (
                record.intended_state_sha256 != state_sha256
                or record.semantic_binding_sha256 != semantic
            ):
                raise CampaignInceptionIntegrityError(
                    "campaign inception authority record does not bind receipt bytes"
                )
            _resolve_gate_and_authorize(
                store=store,
                spec=source_spec,
                prepared=prepared,
                authority_record_sha256=record.record_sha256,
            )
            return _issue_receipt(
                precommit=precommit,
                prepared=prepared,
                spec=source_spec,
                semantic_binding_sha256=semantic,
                state_sha256=state_sha256,
                record=record,
            )

        try:
            history = authority.read_history()
        except MonotonicWorkspaceAuthorityError as exc:
            raise CampaignInceptionIntegrityError(
                "cannot read campaign inception independent authority"
            ) from exc
        latest_commit = next(
            (
                record
                for record in reversed(history)
                if record.phase is AuthorityPhase.COMMIT
            ),
            None,
        )
        if latest_commit is not None:
            if history[-1].phase is AuthorityPhase.PREPARE:
                raise CampaignInceptionIntegrityError(
                    "campaign inception authority has pending state but local receipt is missing"
                )
            prepared_payload = _prepared_payload_from_existing_gate(
                store=store,
                spec=source_spec,
                gate_binding_sha256=gate_binding,
            )
            semantic = _semantic_binding_sha256(
                precommit=precommit,
                spec=source_spec,
                prepared=prepared_payload,
            )
            reconstructed = {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "tx_id": latest_commit.tx_id,
                "semantic_binding_sha256": semantic,
                "precommit": dict(precommit),
                "source_spec": source_spec.payload(),
                "prepared_schedule": prepared_payload,
            }
            state_sha256 = _digest(reconstructed)
            if (
                latest_commit.intended_state_sha256 != state_sha256
                or latest_commit.semantic_binding_sha256 != semantic
            ):
                raise CampaignInceptionIntegrityError(
                    "missing local receipt cannot be reconstructed from committed authority"
                )
            _write_state(state_path, reconstructed)
            try:
                recovery = authority.recover(
                    observed_state_sha256=state_sha256,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise CampaignInceptionIntegrityError(
                    "reconstructed campaign inception receipt is not current"
                ) from exc
            record = _record_for_recovery(recovery)
            if record.record_sha256 != latest_commit.record_sha256:
                raise CampaignInceptionIntegrityError(
                    "reconstructed campaign receipt authority tip changed"
                )
            _resolve_gate_and_authorize(
                store=store,
                spec=source_spec,
                prepared=prepared_payload,
                authority_record_sha256=record.record_sha256,
            )
            return _issue_receipt(
                precommit=precommit,
                prepared=prepared_payload,
                spec=source_spec,
                semantic_binding_sha256=semantic,
                state_sha256=state_sha256,
                record=record,
            )

        try:
            authority.recover(observed_state_sha256=None)
        except MonotonicWorkspaceAuthorityError as exc:
            raise CampaignInceptionIntegrityError(
                "campaign inception local state is missing or rolled back"
            ) from exc

        try:
            prepared = _CANONICAL_PRESTART_PREPARER(
                store,
                expected_store_path=source_spec.expected_store_path,
                expected_source_id=source_spec.source_id,
                expected_run_id=source_spec.run_id,
                expected_stream_epoch=source_spec.stream_epoch,
                anchor_at=source_spec.anchor_at,
                interval_seconds=source_spec.interval_seconds,
                max_items=source_spec.max_items,
                evaluation_start_slot_ordinal=source_spec.evaluation_start_slot_ordinal,
                evaluation_end_slot_ordinal=source_spec.evaluation_end_slot_ordinal,
                gate_binding_sha256=gate_binding,
            )
        except ScheduledSourceUniverseError as exc:
            raise CampaignInceptionIntegrityError(
                "cannot prepare collector START barrier for campaign inception"
            ) from exc
        if type(prepared) is not PreparedScheduledSourceUniverse:
            raise CampaignInceptionIntegrityError(
                "pre-START schedule resolver returned noncanonical type"
            )
        payload = _new_state_payload(
            precommit=precommit,
            spec=source_spec,
            prepared=prepared,
        )
        tx_id, semantic, prepared_payload = _validate_state(
            payload,
            precommit=precommit,
            spec=source_spec,
        )
        state_sha256 = _digest(payload)
        try:
            authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=None,
                intended_state_sha256=state_sha256,
                semantic_binding_sha256=semantic,
            )
            _write_state(state_path, payload)
            record = authority.commit(
                tx_id=tx_id,
                observed_state_sha256=state_sha256,
                semantic_binding_sha256=semantic,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise CampaignInceptionIntegrityError(
                "cannot durably commit campaign inception authority"
            ) from exc
        _resolve_gate_and_authorize(
            store=store,
            spec=source_spec,
            prepared=prepared_payload,
            authority_record_sha256=record.record_sha256,
        )
        return _issue_receipt(
            precommit=precommit,
            prepared=prepared_payload,
            spec=source_spec,
            semantic_binding_sha256=semantic,
            state_sha256=state_sha256,
            record=record,
        )


__all__ = [
    "CampaignInceptionConflictError",
    "CampaignInceptionError",
    "CampaignInceptionIntegrityError",
    "CampaignInceptionReceipt",
    "CampaignInceptionSourceSpec",
    "establish_campaign_inception",
]


def _seal_campaign_inception_dispatch() -> None:
    """Seal campaign-inception positive dispatch against ordinary runtime rebinding.

    The seal composes the already-selected precommit, schedule, monotonic-authority,
    lock and receipt authorities. It creates no independent trust root.
    """

    module_globals = globals()
    expected_error_type = CampaignInceptionIntegrityError
    expected_store_type = CollectorDeltaStore
    expected_locator_type = ForwardUniversePrecommitLocator
    expected_spec_type = CampaignInceptionSourceSpec
    expected_receipt_type = CampaignInceptionReceipt
    expected_prepared_type = PreparedScheduledSourceUniverse
    expected_manifest_type = CampaignPrecommitManifest
    expected_witness_type = CampaignPrecommitPublicationWitness
    expected_authority_type = MonotonicWorkspaceAuthority
    expected_lock_type = WorkspaceEconomicLock
    expected_path_type = Path
    expected_path_equality = _CANONICAL_PATH_EQUALITY
    expected_path_equality_code = getattr(expected_path_equality, "__code__", None)
    expected_path_fspath = _CANONICAL_PATH_FSPATH
    expected_path_fspath_code = getattr(expected_path_fspath, "__code__", None)
    expected_canonical_abspath = _CANONICAL_ABSPATH
    expected_canonical_abspath_code = getattr(
        expected_canonical_abspath, "__code__", None
    )
    expected_inspect = inspect
    expected_getattr_static = inspect.getattr_static
    expected_getattr_static_code = getattr(expected_getattr_static, "__code__", None)
    expected_hashlib = hashlib
    expected_sha256 = hashlib.sha256
    expected_json = json
    expected_json_dumps = json.dumps
    expected_json_dumps_code = getattr(expected_json_dumps, "__code__", None)
    expected_os = os
    expected_os_path = os.path
    expected_os_fspath = os.fspath
    expected_abspath = os.path.abspath
    expected_os_open = os.open
    expected_os_fdopen = os.fdopen
    expected_os_fsync = os.fsync
    expected_os_replace = os.replace
    expected_os_close = os.close
    expected_os_stat = os.stat
    expected_os_fstat = os.fstat
    expected_os_read = os.read
    expected_stat = stat
    expected_stat_isreg = stat.S_ISREG
    expected_uuid = uuid
    expected_uuid4 = uuid.uuid4
    expected_math = math
    expected_isfinite = math.isfinite
    expected_strict_json_loads = strict_json_loads
    expected_strict_json_loads_code = getattr(
        expected_strict_json_loads, "__code__", None
    )
    expected_schema = SCHEMA
    expected_schema_version = SCHEMA_VERSION
    expected_authority_domain = AUTHORITY_DOMAIN
    expected_state_dir = _STATE_DIR
    expected_hex = _HEX
    expected_max_state_bytes = _MAX_STATE_BYTES
    expected_store_seam_names = _CANONICAL_STORE_SEAMS
    expected_store_seam_map = _CANONICAL_STORE_CLASS_SEAMS

    def underlying(value: object) -> object:
        return getattr(value, "__func__", value)

    alias_witnesses = tuple(
        (
            name,
            value,
            underlying(value),
            getattr(underlying(value), "__code__", None),
        )
        for name, value in (
            ("_CANONICAL_WITNESS_RESOLVER", _CANONICAL_WITNESS_RESOLVER),
            ("_CANONICAL_MANIFEST_LOADER", _CANONICAL_MANIFEST_LOADER),
            ("_CANONICAL_PRESTART_PREPARER", _CANONICAL_PRESTART_PREPARER),
            ("_CANONICAL_NEXT_SLOT", _CANONICAL_NEXT_SLOT),
            ("_CANONICAL_GATE_STATUS", _CANONICAL_GATE_STATUS),
            ("_CANONICAL_GATE_AUTHORIZE", _CANONICAL_GATE_AUTHORIZE),
            ("_CANONICAL_SCHEDULE_ID", _CANONICAL_SCHEDULE_ID),
            ("_CANONICAL_SCHEDULE_DUE_AT", _CANONICAL_SCHEDULE_DUE_AT),
            ("_CANONICAL_PATH_EQUALITY", _CANONICAL_PATH_EQUALITY),
            ("_CANONICAL_PATH_FSPATH", _CANONICAL_PATH_FSPATH),
            ("_CANONICAL_OS_FSPATH", _CANONICAL_OS_FSPATH),
            ("_CANONICAL_ABSPATH", _CANONICAL_ABSPATH),
            ("_CANONICAL_STATE_READ_OPEN", _CANONICAL_STATE_READ_OPEN),
        )
    )
    helper_witnesses = tuple(
        (name, value, getattr(value, "__code__", None))
        for name, value in (
            ("_text", _text),
            ("_sha256", _sha256),
            ("_instant", _instant),
            ("_canonical_bytes", _canonical_bytes),
            ("_digest", _digest),
            ("_campaign_key", _campaign_key),
            ("_state_path", _state_path),
            ("_fsync_directory", _fsync_directory),
            ("_write_state", _write_state),
            ("_read_stable_state_bytes", _read_stable_state_bytes),
            ("_read_state", _read_state),
            ("_require_store_seams", _require_store_seams),
            ("_resolve_precommit", _resolve_precommit),
            ("_precommit_payload", _precommit_payload),
            ("_validate_schedule_window", _validate_schedule_window),
            ("_gate_binding_sha256", _gate_binding_sha256),
            ("_semantic_binding_sha256", _semantic_binding_sha256),
            ("_new_state_payload", _new_state_payload),
            ("_validate_state", _validate_state),
            ("_authority", _authority),
            ("_record_for_recovery", _record_for_recovery),
            ("_expected_schedule_id", _expected_schedule_id),
            (
                "_canonical_absolute_path_text",
                _canonical_absolute_path_text,
            ),
            (
                "_prepared_payload_from_existing_gate",
                _prepared_payload_from_existing_gate,
            ),
            ("_require_store_seams_class_only", _require_store_seams_class_only),
            ("_resolve_gate_and_authorize", _resolve_gate_and_authorize),
            ("_issue_receipt", _issue_receipt),
        )
    )
    store_seam_witnesses = tuple(
        (
            name,
            surface,
            underlying(surface),
            getattr(underlying(surface), "__code__", None),
        )
        for name, surface in sorted(expected_store_seam_map.items())
    )

    def class_surface_witness(owner: object, name: str) -> tuple[object, str, object, object, object]:
        surface = expected_getattr_static(owner, name)
        candidate = getattr(surface, "__func__", None)
        if candidate is None and isinstance(surface, property):
            candidate = surface.fget
        if candidate is None:
            candidate = surface
        return owner, name, surface, candidate, getattr(candidate, "__code__", None)

    dynamic_surface_witnesses = tuple(
        class_surface_witness(owner, name)
        for owner, names in (
            (
                expected_path_type,
                ("__new__", "__eq__", "__fspath__"),
            ),
            (
                expected_authority_type,
                ("__init__", "prepare", "commit", "recover", "read_history"),
            ),
            (
                expected_lock_type,
                ("__init__", "acquire", "release", "__enter__", "__exit__"),
            ),
            (
                expected_spec_type,
                ("__post_init__", "payload"),
            ),
            (
                expected_receipt_type,
                ("_issue", "to_dict"),
            ),
            (
                expected_prepared_type,
                ("_issue", "to_dict"),
            ),
            (
                expected_locator_type,
                ("__post_init__", "absolute_manifest_path"),
            ),
        )
        for name in names
    )

    original_establish = establish_campaign_inception
    original_establish_code = original_establish.__code__

    def require_dispatch_integrity() -> None:
        for name, expected in (
            ("CampaignInceptionIntegrityError", expected_error_type),
            ("CollectorDeltaStore", expected_store_type),
            ("ForwardUniversePrecommitLocator", expected_locator_type),
            ("CampaignInceptionSourceSpec", expected_spec_type),
            ("CampaignInceptionReceipt", expected_receipt_type),
            ("PreparedScheduledSourceUniverse", expected_prepared_type),
            ("CampaignPrecommitManifest", expected_manifest_type),
            ("CampaignPrecommitPublicationWitness", expected_witness_type),
            ("MonotonicWorkspaceAuthority", expected_authority_type),
            ("WorkspaceEconomicLock", expected_lock_type),
            ("Path", expected_path_type),
        ):
            if module_globals.get(name) is not expected:
                raise expected_error_type(
                    "campaign inception type dispatch authority is rebound: " + name
                )

        if (
            module_globals.get("SCHEMA") != expected_schema
            or module_globals.get("SCHEMA_VERSION") != expected_schema_version
            or module_globals.get("AUTHORITY_DOMAIN") != expected_authority_domain
            or module_globals.get("_STATE_DIR") != expected_state_dir
            or module_globals.get("_HEX") is not expected_hex
            or module_globals.get("_MAX_STATE_BYTES") != expected_max_state_bytes
        ):
            raise expected_error_type("campaign inception schema/domain authority is rebound")

        if (
            module_globals.get("_CANONICAL_PATH_EQUALITY")
            is not expected_path_equality
            or getattr(expected_path_equality, "__code__", None)
            is not expected_path_equality_code
            or module_globals.get("_CANONICAL_PATH_FSPATH")
            is not expected_path_fspath
            or getattr(expected_path_fspath, "__code__", None)
            is not expected_path_fspath_code
            or module_globals.get("_CANONICAL_OS_FSPATH")
            is not expected_os_fspath
            or module_globals.get("_CANONICAL_ABSPATH")
            is not expected_canonical_abspath
            or getattr(expected_canonical_abspath, "__code__", None)
            is not expected_canonical_abspath_code
        ):
            raise expected_error_type(
                "campaign inception path dispatch authority is rebound or mutated"
            )

        if (
            module_globals.get("inspect") is not expected_inspect
            or expected_inspect.getattr_static is not expected_getattr_static
            or getattr(expected_getattr_static, "__code__", None)
            is not expected_getattr_static_code
        ):
            raise expected_error_type(
                "campaign inception reflection dispatch authority is rebound"
            )
        if (
            module_globals.get("hashlib") is not expected_hashlib
            or expected_hashlib.sha256 is not expected_sha256
        ):
            raise expected_error_type(
                "campaign inception digest dispatch authority is rebound"
            )
        if (
            module_globals.get("json") is not expected_json
            or expected_json.dumps is not expected_json_dumps
            or getattr(expected_json_dumps, "__code__", None)
            is not expected_json_dumps_code
        ):
            raise expected_error_type(
                "campaign inception canonical JSON dispatch authority is rebound"
            )
        if (
            module_globals.get("os") is not expected_os
            or expected_os.path is not expected_os_path
            or expected_os.fspath is not expected_os_fspath
            or expected_os_path.abspath is not expected_abspath
            or expected_os.open is not expected_os_open
            or expected_os.fdopen is not expected_os_fdopen
            or expected_os.fsync is not expected_os_fsync
            or expected_os.replace is not expected_os_replace
            or expected_os.close is not expected_os_close
            or expected_os.stat is not expected_os_stat
            or expected_os.fstat is not expected_os_fstat
            or expected_os.read is not expected_os_read
        ):
            raise expected_error_type(
                "campaign inception filesystem dispatch authority is rebound"
            )
        if (
            module_globals.get("stat") is not expected_stat
            or expected_stat.S_ISREG is not expected_stat_isreg
        ):
            raise expected_error_type(
                "campaign inception file-type dispatch authority is rebound"
            )
        if (
            module_globals.get("uuid") is not expected_uuid
            or expected_uuid.uuid4 is not expected_uuid4
            or module_globals.get("math") is not expected_math
            or expected_math.isfinite is not expected_isfinite
        ):
            raise expected_error_type(
                "campaign inception identity/number dispatch authority is rebound"
            )
        if (
            module_globals.get("strict_json_loads") is not expected_strict_json_loads
            or getattr(expected_strict_json_loads, "__code__", None)
            is not expected_strict_json_loads_code
        ):
            raise expected_error_type(
                "campaign inception strict JSON authority is rebound"
            )

        if module_globals.get("_CANONICAL_STORE_SEAMS") is not expected_store_seam_names:
            raise expected_error_type(
                "campaign inception store seam-name authority is rebound"
            )
        current_seam_map = module_globals.get("_CANONICAL_STORE_CLASS_SEAMS")
        if (
            current_seam_map is not expected_store_seam_map
            or type(current_seam_map) is not dict
            or set(current_seam_map) != set(expected_store_seam_names)
        ):
            raise expected_error_type(
                "campaign inception store seam witness map is rebound"
            )

        for name, expected_surface, expected_callable, expected_code in store_seam_witnesses:
            current_surface = expected_getattr_static(expected_store_type, name, None)
            current_callable = underlying(current_surface)
            if (
                current_seam_map.get(name) is not expected_surface
                or current_surface is not expected_surface
                or current_callable is not expected_callable
                or getattr(current_callable, "__code__", None) is not expected_code
            ):
                raise expected_error_type(
                    "campaign inception collector seam authority drifted: " + name
                )

        for name, expected_alias, expected_callable, expected_code in alias_witnesses:
            current_alias = module_globals.get(name)
            if (
                current_alias is not expected_alias
                or underlying(current_alias) is not expected_callable
                or getattr(expected_callable, "__code__", None) is not expected_code
            ):
                raise expected_error_type(
                    "campaign inception canonical dispatch authority is rebound: " + name
                )

        for name, expected_helper, expected_code in helper_witnesses:
            if (
                module_globals.get(name) is not expected_helper
                or getattr(expected_helper, "__code__", None) is not expected_code
            ):
                raise expected_error_type(
                    "campaign inception helper dispatch authority is rebound: " + name
                )

        for owner, name, expected_surface, expected_callable, expected_code in dynamic_surface_witnesses:
            current_surface = expected_getattr_static(owner, name, None)
            current_callable = getattr(current_surface, "__func__", None)
            if current_callable is None and isinstance(current_surface, property):
                current_callable = current_surface.fget
            if current_callable is None:
                current_callable = current_surface
            if (
                current_surface is not expected_surface
                or current_callable is not expected_callable
                or getattr(current_callable, "__code__", None) is not expected_code
            ):
                raise expected_error_type(
                    "campaign inception dynamic method authority drifted: "
                    + owner.__name__
                    + "."
                    + name
                )

        if original_establish.__code__ is not original_establish_code:
            raise expected_error_type(
                "campaign inception establishment executable changed"
            )

    def sealed_establish_campaign_inception(
        *,
        precommit_locator: ForwardUniversePrecommitLocator,
        store: CollectorDeltaStore,
        source_spec: CampaignInceptionSourceSpec,
    ) -> CampaignInceptionReceipt:
        if (
            module_globals.get("establish_campaign_inception")
            is not sealed_establish_campaign_inception
        ):
            raise expected_error_type(
                "campaign inception public establishment authority is rebound"
            )
        require_dispatch_integrity()
        result = original_establish(
            precommit_locator=precommit_locator,
            store=store,
            source_spec=source_spec,
        )
        require_dispatch_integrity()
        if type(result) is not expected_receipt_type:
            raise expected_error_type(
                "campaign inception establishment returned noncanonical receipt type"
            )
        return result

    sealed_establish_campaign_inception.__name__ = original_establish.__name__
    sealed_establish_campaign_inception.__qualname__ = original_establish.__qualname__
    sealed_establish_campaign_inception.__doc__ = original_establish.__doc__
    sealed_establish_campaign_inception.__module__ = original_establish.__module__
    sealed_establish_campaign_inception.__annotations__ = dict(
        original_establish.__annotations__
    )
    module_globals["establish_campaign_inception"] = sealed_establish_campaign_inception


_seal_campaign_inception_dispatch()
del _seal_campaign_inception_dispatch
