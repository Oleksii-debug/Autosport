from __future__ import annotations

"""Freeze campaign-denomination witness location in durable registry authority.

The generic PAPER execution witness root may be selected by supported environment
configuration.  Campaign denomination issuance is different: once a finalized
campaign has acquired positive denomination authority, changing that configuration
must not silently start a second issuance ancestry for the same registry/campaign.

Persist one strict root record beside the existing denomination cache before the
first campaign witness append.  Subsequent reads ignore later environment changes
and re-resolve the originally pinned root.  This reuses ScientificRegistry as the
existing canonical authority; it does not introduce another denomination store.
"""

import hashlib
import os
from pathlib import Path
from typing import Mapping

from . import campaign_economic_authority as _impl
from .integrity import atomic_write_json


_ROOT_STATE_KEY = "campaign_denomination_witness_root"
_ROOT_SCHEMA = "autosport.campaign_denomination_witness_root"
_ROOT_SCHEMA_VERSION = 1
_ROOT_KEYS = {
    "schema",
    "schema_version",
    "registry_identity",
    "root",
    "root_sha256",
}

_ORIGINAL_ISSUANCE_WITNESS_PATH = None
_ORIGINAL_APPEND_ISSUANCE_WITNESS = None


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _root_text(path: Path) -> str:
    try:
        resolved = path.expanduser().resolve(strict=False)
    except OSError as exc:
        raise _impl.CampaignEconomicAuthorityError(
            "cannot resolve campaign denomination witness root"
        ) from exc
    return os.path.normcase(os.path.abspath(os.fspath(resolved)))


def _root_sha256(root: str) -> str:
    return hashlib.sha256(root.encode("utf-8")).hexdigest()


def _validated_root(value: object, *, registry) -> Path:
    if type(value) is not str or not value or value != value.strip():
        raise _impl.CampaignEconomicAuthorityError(
            "campaign denomination witness root is invalid"
        )
    canonical = _root_text(Path(value))
    if value != canonical:
        raise _impl.CampaignEconomicAuthorityError(
            "campaign denomination witness root is not canonical"
        )
    root = Path(canonical)
    try:
        workspace = registry.path.expanduser().resolve(strict=False).parent
        root_resolved = root.resolve(strict=False)
    except OSError as exc:
        raise _impl.CampaignEconomicAuthorityError(
            "cannot resolve campaign denomination witness root boundary"
        ) from exc
    if root_resolved == workspace or _is_within(root_resolved, workspace):
        raise _impl.CampaignEconomicAuthorityError(
            "campaign denomination witness root must remain outside registry workspace"
        )
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise _impl.CampaignEconomicAuthorityError(
            "cannot establish pinned campaign denomination witness root"
        ) from exc
    return root


def _root_record(registry) -> Mapping[str, object] | None:
    state = registry._read()
    raw = state.get(_ROOT_STATE_KEY)
    if raw is None:
        return None
    if type(raw) is not dict or set(raw) != _ROOT_KEYS:
        raise _impl.CampaignEconomicAuthorityError(
            "campaign denomination witness root record is malformed"
        )
    if raw["schema"] != _ROOT_SCHEMA or raw["schema_version"] != _ROOT_SCHEMA_VERSION:
        raise _impl.CampaignEconomicAuthorityError(
            "campaign denomination witness root schema is unsupported"
        )
    if raw["registry_identity"] != _impl._registry_identity(registry.path):
        raise _impl.CampaignEconomicAuthorityError(
            "campaign denomination witness root belongs to another registry"
        )
    root_value = raw["root"]
    root = _validated_root(root_value, registry=registry)
    if raw["root_sha256"] != _root_sha256(str(root)):
        raise _impl.CampaignEconomicAuthorityError(
            "campaign denomination witness root digest mismatch"
        )
    return raw


def _pinned_root(registry) -> Path | None:
    record = _root_record(registry)
    if record is None:
        return None
    return _validated_root(record["root"], registry=registry)


def _ensure_pinned_root(registry) -> Path:
    pinned = _pinned_root(registry)
    if pinned is not None:
        return pinned

    # issue_denomination_binding owns WorkspaceEconomicLock while the witness append
    # executes.  Pin the exact root before writing the first witness so a crash can
    # leave at most a harmless root-only prefix, never an unpinned positive witness.
    try:
        selected = _impl._authority_root(registry.path)
    except Exception as exc:
        raise _impl.CampaignEconomicAuthorityError(
            "cannot select campaign denomination witness root"
        ) from exc
    selected_text = _root_text(selected)
    _validated_root(selected_text, registry=registry)

    state = registry._read()
    existing = state.get(_ROOT_STATE_KEY)
    if existing is not None:
        # Re-resolve rather than overwrite if another valid publication won.
        pinned = _pinned_root(registry)
        if pinned is None:
            raise _impl.CampaignEconomicAuthorityError(
                "campaign denomination witness root disappeared during publication"
            )
        return pinned

    state[_ROOT_STATE_KEY] = {
        "schema": _ROOT_SCHEMA,
        "schema_version": _ROOT_SCHEMA_VERSION,
        "registry_identity": _impl._registry_identity(registry.path),
        "root": selected_text,
        "root_sha256": _root_sha256(selected_text),
    }
    atomic_write_json(registry.path, state)

    pinned = _pinned_root(registry)
    if pinned is None or _root_text(pinned) != selected_text:
        raise _impl.CampaignEconomicAuthorityError(
            "campaign denomination witness root was not durably re-resolved"
        )
    return pinned


def _issuance_witness_path(registry) -> Path:
    pinned = _pinned_root(registry)
    if pinned is None:
        assert _ORIGINAL_ISSUANCE_WITNESS_PATH is not None
        return _ORIGINAL_ISSUANCE_WITNESS_PATH(registry)
    return pinned / (
        f"{_impl._registry_identity(registry.path)}"
        f"{_impl._ISSUANCE_WITNESS_SUFFIX}"
    )


def _append_issuance_witness(
    registry,
    *,
    binding_key: str,
    binding_payload_sha256: str,
    available_at,
):
    _ensure_pinned_root(registry)
    assert _ORIGINAL_APPEND_ISSUANCE_WITNESS is not None
    return _ORIGINAL_APPEND_ISSUANCE_WITNESS(
        registry,
        binding_key=binding_key,
        binding_payload_sha256=binding_payload_sha256,
        available_at=available_at,
    )


def _install() -> None:
    if getattr(_impl, "_campaign_denomination_witness_root_pin_installed", False):
        return
    global _ORIGINAL_ISSUANCE_WITNESS_PATH, _ORIGINAL_APPEND_ISSUANCE_WITNESS
    _ORIGINAL_ISSUANCE_WITNESS_PATH = _impl._issuance_witness_path
    _ORIGINAL_APPEND_ISSUANCE_WITNESS = _impl._append_issuance_witness
    _impl._issuance_witness_path = _issuance_witness_path
    _impl._append_issuance_witness = _append_issuance_witness
    _impl._campaign_denomination_witness_root_pin_installed = True


_install()
