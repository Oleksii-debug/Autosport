"""Durable publication receipt for structurally validated fixed-N risk membership.

This module deliberately proves only that exact fixed-N membership bytes were
published inside one Autosport workspace and fenced by the existing external
MonotonicWorkspaceAuthority.  It does not prove that outcomes were unknown,
that planned runs had not already begun, or that the sample is IID.

Positive causal-precommit authority remains a later composition: the receipt
must be re-resolved against canonical member-run ancestry and product-owned
outcome availability before risk consumers may treat it as prospective proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .integrity import atomic_write_json, durable_path_lock
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .risk_sampling_membership import (
    ResolvedFixedNRiskMembership,
    inspect_fixed_n_risk_membership_structure,
)


_SCHEMA: Final = "autosport.risk.fixed-n-membership-publication"
_SCHEMA_VERSION: Final = 1
_AUTHORITY_DOMAIN: Final = "autosport.risk.fixed-n-membership-publication.v1"
# The publication state is a bounded authority receipt, not an arbitrary corpus.
# Keep the ceiling intentionally generous so it is an availability fence rather
# than a scientific fixed-N limit; canonical membership semantics remain unchanged.
_MAX_STATE_BYTES: Final = 64 * 1024 * 1024


class RiskMembershipPublicationError(RuntimeError):
    """Fixed-N membership publication cannot be issued or re-resolved safely."""


@dataclass(frozen=True, slots=True)
class RiskMembershipPublicationReceipt:
    """Descriptive receipt; callers must re-resolve it before authority use."""

    workspace_instance_id: str
    membership_sha256: str
    authority_generation: int
    authority_record_sha256: str
    state_sha256: str
    research_protocol_id: str
    protocol_sha256: str
    protocol_record_sha256: str
    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    dataset_record_sha256: str
    causal_cutoff: str
    outcome_reveal_after: str
    planned_run_ids: tuple[str, ...]
    sampling_frame_sha256: str
    design_sha256: str
    receipt_sha256: str

    @property
    def causal_precommit_proven(self) -> bool:
        """Publication order alone is not member-run/outcome chronology proof."""
        return False

    @property
    def member_run_ancestry_proven(self) -> bool:
        return False

    @property
    def historical_outcome_unavailability_proven(self) -> bool:
        return False

    @property
    def iid_qualified(self) -> bool:
        return False


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
        raise RiskMembershipPublicationError(
            "fixed-N publication value is outside canonical JSON domain"
        ) from exc


def _pretty_bytes(value: dict[str, object]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RiskMembershipPublicationError(
            "fixed-N publication state is outside canonical JSON domain"
        ) from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _membership_payload(
    membership: ResolvedFixedNRiskMembership,
) -> dict[str, object]:
    if not isinstance(membership, ResolvedFixedNRiskMembership):
        raise RiskMembershipPublicationError(
            "membership must be a structurally resolved fixed-N membership"
        )
    return {
        "research_protocol_id": membership.research_protocol_id,
        "protocol_sha256": membership.protocol_sha256,
        "protocol_record_sha256": membership.protocol_record_sha256,
        "dataset_snapshot_id": membership.dataset_snapshot_id,
        "dataset_manifest_sha256": membership.dataset_manifest_sha256,
        "dataset_record_sha256": membership.dataset_record_sha256,
        "causal_cutoff": membership.causal_cutoff,
        "outcome_reveal_after": membership.outcome_reveal_after,
        "precommitted_at": membership.precommitted_at,
        "planned_run_ids": list(membership.planned_run_ids),
        "sampling_frame_sha256": membership.sampling_frame_sha256,
        "design_sha256": membership.design_sha256,
        "risk_method": membership.risk_method,
    }


def _workspace_and_registry(
    workspace: str | Path,
    registry_path: str | Path,
) -> tuple[Path, Path]:
    workspace_path = Path(workspace).expanduser()
    registry = Path(registry_path).expanduser()
    if not workspace_path.is_absolute() or not registry.is_absolute():
        raise RiskMembershipPublicationError(
            "workspace and registry_path must be absolute paths"
        )
    try:
        resolved_workspace = workspace_path.resolve(strict=False)
        resolved_registry = registry.resolve(strict=True)
    except OSError as exc:
        raise RiskMembershipPublicationError(
            "workspace or ScientificRegistry path cannot be resolved"
        ) from exc
    if not resolved_registry.is_relative_to(resolved_workspace):
        raise RiskMembershipPublicationError(
            "ScientificRegistry must be inside the protected workspace"
        )
    if not resolved_registry.is_file():
        raise RiskMembershipPublicationError(
            "ScientificRegistry path must be an existing regular file"
        )
    return resolved_workspace, resolved_registry


def _state_path(workspace: Path, membership_sha256: str) -> Path:
    return workspace / f".risk-fixed-n-membership-{membership_sha256}.json"


def _path_exists_nofollow(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RiskMembershipPublicationError(
            "fixed-N membership publication state path cannot be inspected"
        ) from exc
    return True


def _binding_sha256(
    *,
    workspace_instance_id: str,
    membership_sha256: str,
    state_sha256: str,
) -> str:
    payload = {
        "authority_domain": _AUTHORITY_DOMAIN,
        "workspace_instance_id": workspace_instance_id,
        "membership_sha256": membership_sha256,
        "state_sha256": state_sha256,
    }
    return _sha256_bytes(_canonical_bytes(payload))


def _read_stable_state_bytes(path: Path) -> bytes:
    try:
        before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RiskMembershipPublicationError(
                "fixed-N membership publication state must be one regular file"
            )
        if before.st_size < 0 or before.st_size > _MAX_STATE_BYTES:
            raise RiskMembershipPublicationError(
                "fixed-N membership publication state exceeds bounded size"
            )
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
            ):
                raise RiskMembershipPublicationError(
                    "fixed-N membership publication state changed during open"
                )
            if opened.st_size < 0 or opened.st_size > _MAX_STATE_BYTES:
                raise RiskMembershipPublicationError(
                    "fixed-N membership publication state exceeds bounded size"
                )
            payload = handle.read(_MAX_STATE_BYTES + 1)
            if len(payload) > _MAX_STATE_BYTES:
                raise RiskMembershipPublicationError(
                    "fixed-N membership publication state exceeds bounded size"
                )
            after_open = os.fstat(handle.fileno())
        after = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise RiskMembershipPublicationError(
            "fixed-N membership publication state is unreadable"
        ) from exc
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    )
    # Keep filesystem-view comparisons within the same API family.  Windows may
    # expose timestamp metadata with different representation through path stat()
    # and handle fstat() even when dev/ino/nlink prove that both views name the
    # same file.  Cross-comparing those timestamps therefore creates a false
    # TOCTOU failure.  We already bind path -> opened handle above by exact
    # dev/ino/nlink; now require both the path view and the opened-handle view to
    # remain internally stable across the read.
    if identity(before) != identity(after) or identity(opened) != identity(after_open):
        raise RiskMembershipPublicationError(
            "fixed-N membership publication state changed during stable read"
        )
    return payload


def _decode_state(
    path: Path,
    *,
    expected: dict[str, object],
) -> str:
    try:
        raw_bytes = _read_stable_state_bytes(path)
        raw = strict_json_loads(raw_bytes.decode("utf-8"))
    except RiskMembershipPublicationError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise RiskMembershipPublicationError(
            "fixed-N membership publication state is unreadable or invalid"
        ) from exc
    if type(raw) is not dict or raw != expected:
        raise RiskMembershipPublicationError(
            "fixed-N membership publication state conflicts with exact membership"
        )
    expected_bytes = _pretty_bytes(expected)
    if raw_bytes != expected_bytes:
        raise RiskMembershipPublicationError(
            "fixed-N membership publication state is not canonically serialized"
        )
    return _sha256_bytes(raw_bytes)


def _receipt(
    membership: ResolvedFixedNRiskMembership,
    *,
    membership_sha256: str,
    state_sha256: str,
    authority: MonotonicWorkspaceAuthority,
    authority_record,
) -> RiskMembershipPublicationReceipt:
    if authority_record is None or authority_record.phase is not AuthorityPhase.COMMIT:
        raise RiskMembershipPublicationError(
            "fixed-N membership publication lacks a committed monotonic record"
        )
    if authority_record.intended_state_sha256 != state_sha256:
        raise RiskMembershipPublicationError(
            "monotonic authority is not bound to the exact publication state"
        )
    core = {
        "workspace_instance_id": authority.workspace_instance_id,
        "membership_sha256": membership_sha256,
        "authority_generation": authority_record.generation,
        "authority_record_sha256": authority_record.record_sha256,
        "state_sha256": state_sha256,
    }
    receipt_sha256 = _sha256_bytes(_canonical_bytes(core))
    return RiskMembershipPublicationReceipt(
        workspace_instance_id=authority.workspace_instance_id,
        membership_sha256=membership_sha256,
        authority_generation=authority_record.generation,
        authority_record_sha256=authority_record.record_sha256,
        state_sha256=state_sha256,
        research_protocol_id=membership.research_protocol_id,
        protocol_sha256=membership.protocol_sha256,
        protocol_record_sha256=membership.protocol_record_sha256,
        dataset_snapshot_id=membership.dataset_snapshot_id,
        dataset_manifest_sha256=membership.dataset_manifest_sha256,
        dataset_record_sha256=membership.dataset_record_sha256,
        causal_cutoff=membership.causal_cutoff,
        outcome_reveal_after=membership.outcome_reveal_after,
        planned_run_ids=membership.planned_run_ids,
        sampling_frame_sha256=membership.sampling_frame_sha256,
        design_sha256=membership.design_sha256,
        receipt_sha256=receipt_sha256,
    )


def publish_fixed_n_membership_structure(
    registry_path: str | Path,
    *,
    workspace: str | Path,
    research_protocol_id: str,
    dataset_snapshot_id: str,
    authority_root: str | Path | None = None,
) -> RiskMembershipPublicationReceipt:
    """Publish exact structural membership under the existing monotonic authority.

    The returned receipt proves workspace-scoped durable publication/rollback
    ordering only.  It never upgrades causal_precommit_proven or iid_qualified.
    """

    workspace_path, registry = _workspace_and_registry(workspace, registry_path)
    membership = inspect_fixed_n_risk_membership_structure(
        registry,
        research_protocol_id=research_protocol_id,
        dataset_snapshot_id=dataset_snapshot_id,
    )
    membership_payload = _membership_payload(membership)
    membership_sha256 = _sha256_bytes(_canonical_bytes(membership_payload))
    state_path = _state_path(workspace_path, membership_sha256)

    authority = MonotonicWorkspaceAuthority(
        workspace=workspace_path,
        domain=_AUTHORITY_DOMAIN,
        key=membership_sha256,
        authority_root=authority_root,
    )
    state_payload: dict[str, object] = {
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "workspace_instance_id": authority.workspace_instance_id,
        "membership_sha256": membership_sha256,
        "membership": membership_payload,
    }
    intended_state_sha256 = _sha256_bytes(_pretty_bytes(state_payload))
    semantic_binding_sha256 = _binding_sha256(
        workspace_instance_id=authority.workspace_instance_id,
        membership_sha256=membership_sha256,
        state_sha256=intended_state_sha256,
    )

    try:
        with durable_path_lock(state_path):
            observed = (
                _decode_state(state_path, expected=state_payload)
                if _path_exists_nofollow(state_path)
                else None
            )

            history = authority.read_history()
            pending = (
                history[-1]
                if history and history[-1].phase is AuthorityPhase.PREPARE
                else None
            )
            if pending is not None and (
                pending.intended_state_sha256 != intended_state_sha256
                or pending.semantic_binding_sha256 != semantic_binding_sha256
            ):
                raise RiskMembershipPublicationError(
                    "pending monotonic publication conflicts with exact membership"
                )

            recovery = authority.recover(
                observed_state_sha256=observed,
                tx_id=pending.tx_id if pending is not None else None,
                semantic_binding_sha256=(
                    semantic_binding_sha256 if pending is not None else None
                ),
            )

            if observed is not None:
                record = recovery.record
                if record is None or record.phase is not AuthorityPhase.COMMIT:
                    raise RiskMembershipPublicationError(
                        "published fixed-N membership lacks current monotonic COMMIT"
                    )
                if record.semantic_binding_sha256 != semantic_binding_sha256:
                    raise RiskMembershipPublicationError(
                        "monotonic COMMIT semantic binding does not match membership"
                    )
                return _receipt(
                    membership,
                    membership_sha256=membership_sha256,
                    state_sha256=observed,
                    authority=authority,
                    authority_record=record,
                )

            tx_id = f"fixed-n-membership-{uuid.uuid4().hex}"
            authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=None,
                intended_state_sha256=intended_state_sha256,
                semantic_binding_sha256=semantic_binding_sha256,
            )
            atomic_write_json(state_path, state_payload)
            published = _decode_state(state_path, expected=state_payload)
            if published != intended_state_sha256:
                raise RiskMembershipPublicationError(
                    "published fixed-N membership bytes differ from prepared state"
                )
            record = authority.commit(
                tx_id=tx_id,
                observed_state_sha256=published,
                semantic_binding_sha256=semantic_binding_sha256,
            )
            return _receipt(
                membership,
                membership_sha256=membership_sha256,
                state_sha256=published,
                authority=authority,
                authority_record=record,
            )
    except RiskMembershipPublicationError:
        raise
    except (MonotonicWorkspaceAuthorityError, OSError, ValueError) as exc:
        raise RiskMembershipPublicationError(
            "fixed-N membership publication failed closed"
        ) from exc


def resolve_fixed_n_membership_publication(
    registry_path: str | Path,
    *,
    workspace: str | Path,
    research_protocol_id: str,
    dataset_snapshot_id: str,
    authority_root: str | Path | None = None,
) -> RiskMembershipPublicationReceipt:
    """Re-resolve an already committed receipt without creating publication state.

    A pending PREPARE is deliberately not recovered here.  The publishing path owns
    crash recovery; authority consumers must never turn verification into issuance.
    """

    workspace_path, registry = _workspace_and_registry(workspace, registry_path)
    membership = inspect_fixed_n_risk_membership_structure(
        registry,
        research_protocol_id=research_protocol_id,
        dataset_snapshot_id=dataset_snapshot_id,
    )
    membership_payload = _membership_payload(membership)
    membership_sha256 = _sha256_bytes(_canonical_bytes(membership_payload))
    state_path = _state_path(workspace_path, membership_sha256)
    if not _path_exists_nofollow(state_path):
        raise RiskMembershipPublicationError(
            "fixed-N membership has no existing publication receipt"
        )

    authority = MonotonicWorkspaceAuthority(
        workspace=workspace_path,
        domain=_AUTHORITY_DOMAIN,
        key=membership_sha256,
        authority_root=authority_root,
    )
    state_payload: dict[str, object] = {
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "workspace_instance_id": authority.workspace_instance_id,
        "membership_sha256": membership_sha256,
        "membership": membership_payload,
    }
    intended_state_sha256 = _sha256_bytes(_pretty_bytes(state_payload))
    semantic_binding_sha256 = _binding_sha256(
        workspace_instance_id=authority.workspace_instance_id,
        membership_sha256=membership_sha256,
        state_sha256=intended_state_sha256,
    )

    try:
        with durable_path_lock(state_path):
            observed = _decode_state(state_path, expected=state_payload)
            history = authority.read_history()
            if not history:
                raise RiskMembershipPublicationError(
                    "publication state exists without monotonic authority history"
                )
            if history[-1].phase is AuthorityPhase.PREPARE:
                raise RiskMembershipPublicationError(
                    "publication has pending PREPARE; verification cannot recover issuance"
                )
            recovery = authority.recover(observed_state_sha256=observed)
            record = recovery.record
            if record is None or record.phase is not AuthorityPhase.COMMIT:
                raise RiskMembershipPublicationError(
                    "fixed-N membership publication lacks current monotonic COMMIT"
                )
            if (
                record.intended_state_sha256 != intended_state_sha256
                or record.semantic_binding_sha256 != semantic_binding_sha256
            ):
                raise RiskMembershipPublicationError(
                    "monotonic COMMIT does not bind the exact fixed-N membership"
                )
            return _receipt(
                membership,
                membership_sha256=membership_sha256,
                state_sha256=observed,
                authority=authority,
                authority_record=record,
            )
    except RiskMembershipPublicationError:
        raise
    except (MonotonicWorkspaceAuthorityError, OSError, ValueError) as exc:
        raise RiskMembershipPublicationError(
            "fixed-N membership publication verification failed closed"
        ) from exc
