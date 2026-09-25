"""Product-owned randomization-root precommit for fixed-N risk sampling.

This module closes one narrow prerequisite of the fixed-N IID risk chain: the
randomization root is generated inside Autosport, durably bound to the exact
already-published fixed-N membership, and protected by the existing external
MonotonicWorkspaceAuthority.

It deliberately does *not* claim that member occurrences are product-owned or
that the full experiment is IID-qualified.  A downstream occurrence producer
must consume this exact receipt before it can prove member ancestry/completion.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
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
from .risk_membership_publication import (
    RiskMembershipPublicationError,
    RiskMembershipPublicationReceipt,
    resolve_fixed_n_membership_publication,
)

_SCHEMA: Final = "autosport.risk.randomization-precommit"
_SCHEMA_VERSION: Final = 1
_AUTHORITY_DOMAIN: Final = "autosport.risk.randomization-precommit.v1"
_ROOT_BYTES: Final = 32
_HEX: Final = frozenset("0123456789abcdef")
_PRODUCT_TOKEN_BYTES: Final = secrets.token_bytes


class RiskRandomizationPrecommitError(RuntimeError):
    """Randomization precommit cannot be issued or re-resolved safely."""


@dataclass(frozen=True, slots=True)
class RiskRandomizationPrecommitReceipt:
    workspace_instance_id: str
    experiment_id: str
    membership_sha256: str
    membership_receipt_sha256: str
    randomization_root_sha256: str
    state_sha256: str
    authority_generation: int
    authority_record_sha256: str
    receipt_sha256: str

    @property
    def product_randomization_root_issued(self) -> bool:
        return True

    @property
    def occurrence_ancestry_proven(self) -> bool:
        return False

    @property
    def iid_qualified(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False


def _text(value: object, name: str, *, max_length: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RiskRandomizationPrecommitError(
            f"{name} must be non-empty canonical text"
        )
    if len(value) > max_length or "\x00" in value or any(ord(ch) < 32 for ch in value):
        raise RiskRandomizationPrecommitError(
            f"{name} contains unsupported characters"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RiskRandomizationPrecommitError(f"{name} is not valid UTF-8") from exc
    return value


def _sha256_text(value: object, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(ch not in _HEX for ch in value):
        raise RiskRandomizationPrecommitError(
            f"{name} must be a lowercase SHA-256 digest"
        )
    return value


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
        raise RiskRandomizationPrecommitError(
            "randomization precommit is outside canonical JSON domain"
        ) from exc


def _pretty_bytes(value: object) -> bytes:
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
        raise RiskRandomizationPrecommitError(
            "randomization precommit state is outside canonical JSON domain"
        ) from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _workspace_path(workspace: str | Path) -> Path:
    try:
        path = Path(workspace).expanduser()
    except RuntimeError as exc:
        raise RiskRandomizationPrecommitError("workspace cannot be expanded") from exc
    if not path.is_absolute():
        raise RiskRandomizationPrecommitError("workspace must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RiskRandomizationPrecommitError("workspace cannot be resolved") from exc
    if not resolved.is_dir():
        raise RiskRandomizationPrecommitError("workspace must be an existing directory")
    return resolved


def _experiment_key(experiment_id: str) -> str:
    return hashlib.sha256(
        ("autosport-risk-randomization-experiment-v1\n" + experiment_id).encode("utf-8")
    ).hexdigest()


def _state_path(workspace: Path, experiment_key: str) -> Path:
    return workspace / f".risk-randomization-precommit-{experiment_key}.json"


def _read_regular_bytes(path: Path) -> bytes:
    try:
        before = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RiskRandomizationPrecommitError(
                "randomization precommit state must be one regular file"
            )
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
            ):
                raise RiskRandomizationPrecommitError(
                    "randomization precommit state changed during open"
                )
            payload = handle.read()
            after_open = os.fstat(handle.fileno())
        after = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except RiskRandomizationPrecommitError:
        raise
    except OSError as exc:
        raise RiskRandomizationPrecommitError(
            "randomization precommit state is unreadable"
        ) from exc

    path_identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    )
    handle_identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_nlink,
    )
    if path_identity(before) != path_identity(after) or handle_identity(opened) != handle_identity(after_open):
        raise RiskRandomizationPrecommitError(
            "randomization precommit state changed during stable read"
        )
    return payload


def _membership_binding(receipt: RiskMembershipPublicationReceipt) -> dict[str, object]:
    if type(receipt) is not RiskMembershipPublicationReceipt:
        raise RiskRandomizationPrecommitError(
            "membership publication resolver returned an unsupported receipt"
        )
    return {
        "membership_sha256": _sha256_text(
            receipt.membership_sha256, "membership_sha256"
        ),
        "membership_receipt_sha256": _sha256_text(
            receipt.receipt_sha256, "membership_receipt_sha256"
        ),
        "research_protocol_id": _text(
            receipt.research_protocol_id, "research_protocol_id"
        ),
        "protocol_sha256": _sha256_text(receipt.protocol_sha256, "protocol_sha256"),
        "dataset_snapshot_id": _text(
            receipt.dataset_snapshot_id, "dataset_snapshot_id"
        ),
        "dataset_manifest_sha256": _sha256_text(
            receipt.dataset_manifest_sha256, "dataset_manifest_sha256"
        ),
        "design_sha256": _sha256_text(receipt.design_sha256, "design_sha256"),
        "planned_run_ids": list(receipt.planned_run_ids),
    }


def _state_template(
    *,
    workspace_instance_id: str,
    experiment_id: str,
    membership: dict[str, object],
    randomization_root_sha256: str,
) -> dict[str, object]:
    return {
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "workspace_instance_id": workspace_instance_id,
        "experiment_id": experiment_id,
        "membership": membership,
        "randomization_root_sha256": randomization_root_sha256,
    }


def _decode_state(
    path: Path,
    *,
    workspace_instance_id: str,
    experiment_id: str,
    membership: dict[str, object],
) -> tuple[dict[str, object], str]:
    raw_bytes = _read_regular_bytes(path)
    try:
        raw = strict_json_loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RiskRandomizationPrecommitError(
            "randomization precommit state is invalid JSON"
        ) from exc
    expected_keys = {
        "schema",
        "schema_version",
        "workspace_instance_id",
        "experiment_id",
        "membership",
        "randomization_root_sha256",
    }
    if type(raw) is not dict or set(raw) != expected_keys:
        raise RiskRandomizationPrecommitError(
            "randomization precommit state fields mismatch"
        )
    if (
        raw.get("schema") != _SCHEMA
        or type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != _SCHEMA_VERSION
        or raw.get("workspace_instance_id") != workspace_instance_id
        or raw.get("experiment_id") != experiment_id
        or raw.get("membership") != membership
    ):
        raise RiskRandomizationPrecommitError(
            "randomization precommit conflicts with exact experiment membership"
        )
    _sha256_text(raw.get("randomization_root_sha256"), "randomization_root_sha256")
    if raw_bytes != _pretty_bytes(raw):
        raise RiskRandomizationPrecommitError(
            "randomization precommit state is not canonically serialized"
        )
    return raw, _sha256_bytes(raw_bytes)


def _semantic_binding_sha256(
    *,
    experiment_key: str,
    membership_receipt_sha256: str,
    state_sha256: str,
) -> str:
    return _sha256_bytes(
        _canonical_bytes(
            {
                "authority_domain": _AUTHORITY_DOMAIN,
                "experiment_key": experiment_key,
                "membership_receipt_sha256": membership_receipt_sha256,
                "state_sha256": state_sha256,
            }
        )
    )


def _receipt(
    *,
    state: dict[str, object],
    state_sha256: str,
    authority: MonotonicWorkspaceAuthority,
    authority_record,
) -> RiskRandomizationPrecommitReceipt:
    if authority_record is None or authority_record.phase is not AuthorityPhase.COMMIT:
        raise RiskRandomizationPrecommitError(
            "randomization precommit lacks committed monotonic authority"
        )
    if authority_record.intended_state_sha256 != state_sha256:
        raise RiskRandomizationPrecommitError(
            "monotonic authority is not bound to exact randomization state"
        )
    membership = state["membership"]
    assert type(membership) is dict
    expected_binding = _semantic_binding_sha256(
        experiment_key=_experiment_key(_text(state.get("experiment_id"), "experiment_id")),
        membership_receipt_sha256=_sha256_text(
            membership.get("membership_receipt_sha256"), "membership_receipt_sha256"
        ),
        state_sha256=_sha256_text(state_sha256, "state_sha256"),
    )
    if authority_record.semantic_binding_sha256 != expected_binding:
        raise RiskRandomizationPrecommitError(
            "monotonic authority semantic binding does not match exact randomization state"
        )
    core = {
        "workspace_instance_id": authority.workspace_instance_id,
        "experiment_id": state["experiment_id"],
        "membership_sha256": membership["membership_sha256"],
        "membership_receipt_sha256": membership["membership_receipt_sha256"],
        "randomization_root_sha256": state["randomization_root_sha256"],
        "state_sha256": state_sha256,
        "authority_generation": authority_record.generation,
        "authority_record_sha256": authority_record.record_sha256,
    }
    return RiskRandomizationPrecommitReceipt(
        **core,
        receipt_sha256=_sha256_bytes(_canonical_bytes(core)),
    )


def issue_risk_randomization_precommit(
    registry_path: str | Path,
    *,
    workspace: str | Path,
    research_protocol_id: str,
    dataset_snapshot_id: str,
    experiment_id: str,
    authority_root: str | Path | None = None,
) -> RiskRandomizationPrecommitReceipt:
    """Create-or-recover one product-generated randomization root.

    There is intentionally no caller-supplied root/seed argument.  The existing
    fixed-N membership publication is freshly re-resolved before every issue or
    retry, so a caller cannot bind a root to a self-asserted membership receipt.
    """

    experiment_id = _text(experiment_id, "experiment_id")
    workspace_path = _workspace_path(workspace)
    try:
        membership_receipt = resolve_fixed_n_membership_publication(
            registry_path,
            workspace=workspace_path,
            research_protocol_id=research_protocol_id,
            dataset_snapshot_id=dataset_snapshot_id,
            authority_root=authority_root,
        )
    except RiskMembershipPublicationError as exc:
        raise RiskRandomizationPrecommitError(
            "randomization precommit requires an exact published fixed-N membership"
        ) from exc
    membership = _membership_binding(membership_receipt)
    experiment_key = _experiment_key(experiment_id)
    state_path = _state_path(workspace_path, experiment_key)
    authority = MonotonicWorkspaceAuthority(
        workspace=workspace_path,
        domain=_AUTHORITY_DOMAIN,
        key=experiment_key,
        authority_root=authority_root,
    )

    try:
        with durable_path_lock(state_path):
            existing_state: dict[str, object] | None = None
            observed: str | None = None
            if state_path.exists():
                existing_state, observed = _decode_state(
                    state_path,
                    workspace_instance_id=authority.workspace_instance_id,
                    experiment_id=experiment_id,
                    membership=membership,
                )

            history = authority.read_history()
            pending = (
                history[-1]
                if history and history[-1].phase is AuthorityPhase.PREPARE
                else None
            )

            if existing_state is not None:
                binding = _semantic_binding_sha256(
                    experiment_key=experiment_key,
                    membership_receipt_sha256=str(membership["membership_receipt_sha256"]),
                    state_sha256=observed,
                )
                if pending is not None and (
                    pending.intended_state_sha256 != observed
                    or pending.semantic_binding_sha256 != binding
                ):
                    raise RiskRandomizationPrecommitError(
                        "pending randomization precommit conflicts with exact published state"
                    )
                recovery = authority.recover(
                    observed_state_sha256=observed,
                    tx_id=pending.tx_id if pending is not None else None,
                    semantic_binding_sha256=binding if pending is not None else None,
                )
                if recovery.record is None or recovery.record.phase is not AuthorityPhase.COMMIT:
                    raise RiskRandomizationPrecommitError(
                        "published randomization root lacks committed authority"
                    )
                return _receipt(
                    state=existing_state,
                    state_sha256=observed,
                    authority=authority,
                    authority_record=recovery.record,
                )

            recovery = authority.recover(
                observed_state_sha256=None,
                tx_id=pending.tx_id if pending is not None else None,
                semantic_binding_sha256=(
                    pending.semantic_binding_sha256 if pending is not None else None
                ),
            )
            if recovery.committed_state_sha256 is not None:
                raise RiskRandomizationPrecommitError(
                    "committed randomization state is missing from workspace"
                )

            if secrets.token_bytes is not _PRODUCT_TOKEN_BYTES:
                raise RiskRandomizationPrecommitError(
                    "randomization entropy source was rebound"
                )
            randomization_root_sha256 = hashlib.sha256(
                _PRODUCT_TOKEN_BYTES(_ROOT_BYTES)
            ).hexdigest()
            state = _state_template(
                workspace_instance_id=authority.workspace_instance_id,
                experiment_id=experiment_id,
                membership=membership,
                randomization_root_sha256=randomization_root_sha256,
            )
            intended = _sha256_bytes(_pretty_bytes(state))
            binding = _semantic_binding_sha256(
                experiment_key=experiment_key,
                membership_receipt_sha256=str(membership["membership_receipt_sha256"]),
                state_sha256=intended,
            )
            tx_id = f"risk-randomization-{uuid.uuid4().hex}"
            authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=None,
                intended_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
            atomic_write_json(state_path, state)
            published, published_sha = _decode_state(
                state_path,
                workspace_instance_id=authority.workspace_instance_id,
                experiment_id=experiment_id,
                membership=membership,
            )
            if published_sha != intended:
                raise RiskRandomizationPrecommitError(
                    "published randomization state differs from prepared state"
                )
            record = authority.commit(
                tx_id=tx_id,
                observed_state_sha256=published_sha,
                semantic_binding_sha256=binding,
            )
            return _receipt(
                state=published,
                state_sha256=published_sha,
                authority=authority,
                authority_record=record,
            )
    except RiskRandomizationPrecommitError:
        raise
    except (MonotonicWorkspaceAuthorityError, OSError, ValueError) as exc:
        raise RiskRandomizationPrecommitError(
            "randomization precommit failed closed"
        ) from exc


def resolve_risk_randomization_precommit(
    registry_path: str | Path,
    *,
    workspace: str | Path,
    research_protocol_id: str,
    dataset_snapshot_id: str,
    experiment_id: str,
    authority_root: str | Path | None = None,
) -> RiskRandomizationPrecommitReceipt:
    """Re-resolve a committed root without creating or recovering issuance state."""

    experiment_id = _text(experiment_id, "experiment_id")
    workspace_path = _workspace_path(workspace)
    try:
        membership_receipt = resolve_fixed_n_membership_publication(
            registry_path,
            workspace=workspace_path,
            research_protocol_id=research_protocol_id,
            dataset_snapshot_id=dataset_snapshot_id,
            authority_root=authority_root,
        )
    except RiskMembershipPublicationError as exc:
        raise RiskRandomizationPrecommitError(
            "randomization precommit membership no longer resolves"
        ) from exc
    membership = _membership_binding(membership_receipt)
    experiment_key = _experiment_key(experiment_id)
    state_path = _state_path(workspace_path, experiment_key)
    authority = MonotonicWorkspaceAuthority(
        workspace=workspace_path,
        domain=_AUTHORITY_DOMAIN,
        key=experiment_key,
        authority_root=authority_root,
    )
    try:
        with durable_path_lock(state_path):
            if not state_path.exists():
                raise RiskRandomizationPrecommitError(
                    "randomization precommit state does not exist"
                )
            state, observed = _decode_state(
                state_path,
                workspace_instance_id=authority.workspace_instance_id,
                experiment_id=experiment_id,
                membership=membership,
            )
            history = authority.read_history()
            if history and history[-1].phase is AuthorityPhase.PREPARE:
                raise RiskRandomizationPrecommitError(
                    "randomization precommit has pending PREPARE; issuer recovery required"
                )
            binding = _semantic_binding_sha256(
                experiment_key=experiment_key,
                membership_receipt_sha256=str(membership["membership_receipt_sha256"]),
                state_sha256=observed,
            )
            recovery = authority.recover(observed_state_sha256=observed)
            if (
                recovery.record is None
                or recovery.record.phase is not AuthorityPhase.COMMIT
                or recovery.record.semantic_binding_sha256 != binding
            ):
                raise RiskRandomizationPrecommitError(
                    "randomization precommit monotonic binding is not current"
                )
            return _receipt(
                state=state,
                state_sha256=observed,
                authority=authority,
                authority_record=recovery.record,
            )
    except RiskRandomizationPrecommitError:
        raise
    except (MonotonicWorkspaceAuthorityError, OSError, ValueError) as exc:
        raise RiskRandomizationPrecommitError(
            "randomization precommit resolution failed closed"
        ) from exc


__all__ = [
    "RiskRandomizationPrecommitError",
    "RiskRandomizationPrecommitReceipt",
    "issue_risk_randomization_precommit",
    "resolve_risk_randomization_precommit",
]
