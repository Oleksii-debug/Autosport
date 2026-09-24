from __future__ import annotations

"""Freeze campaign-denomination witness location in durable registry authority.

The generic PAPER execution witness root may be selected by supported environment
configuration. Campaign denomination issuance is different: once a finalized
campaign has acquired positive denomination authority, changing that configuration
must not silently start a second issuance ancestry for the same registry/campaign.

Persist one strict root record beside the existing denomination cache before the
first campaign witness append. Subsequent reads ignore later environment changes
and re-resolve the originally pinned root. The same composition also extends the
existing ScientificRegistry monotonic-byte detector to the two canonical
campaign-denomination extension keys so those bytes cannot be rolled back to the
pre-denomination registry image while machine authority survives.
"""

import hashlib
import os
from pathlib import Path
from typing import Mapping

from . import campaign_economic_authority as _impl
from . import integrity as _integrity
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
_REGISTRY_BASE_KEYS = frozenset({"schema_version", "records"})
_REGISTRY_DENOMINATION_EXTENSION_KEYS = frozenset(
    {_impl._DENOMINATION_STATE_KEY, _ROOT_STATE_KEY}
)

_ORIGINAL_SCIENTIFIC_REGISTRY_DETECTOR = None
_ORIGINAL_CAMPAIGN_ATOMIC_WRITE_JSON = None
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


def _scientific_registry_state_with_denomination(payload: dict[str, object]) -> bool:
    assert _ORIGINAL_SCIENTIFIC_REGISTRY_DETECTOR is not None
    if _ORIGINAL_SCIENTIFIC_REGISTRY_DETECTOR(payload):
        return True
    if type(payload) is not dict:
        return False
    actual = frozenset(payload)
    if not _REGISTRY_BASE_KEYS.issubset(actual):
        return False
    if not actual.issubset(
        _REGISTRY_BASE_KEYS | _REGISTRY_DENOMINATION_EXTENSION_KEYS
    ):
        return False
    if not actual.intersection(_REGISTRY_DENOMINATION_EXTENSION_KEYS):
        return False
    # The existing detector remains the single validator for the scientific record
    # envelope. This wrapper only says that these two exact product extensions are
    # part of the same protected whole-file image rather than generic JSON metadata.
    return _ORIGINAL_SCIENTIFIC_REGISTRY_DETECTOR(
        {
            "schema_version": payload.get("schema_version"),
            "records": payload.get("records"),
        }
    )


def _install_registry_extension_detector() -> None:
    if getattr(
        _integrity,
        "_campaign_denomination_registry_extensions_installed",
        False,
    ):
        return
    global _ORIGINAL_SCIENTIFIC_REGISTRY_DETECTOR
    _ORIGINAL_SCIENTIFIC_REGISTRY_DETECTOR = (
        _integrity._looks_like_scientific_registry_state
    )
    _integrity._looks_like_scientific_registry_state = (
        _scientific_registry_state_with_denomination
    )
    _integrity._campaign_denomination_registry_extensions_installed = True


def _validated_root(value: object, *, registry_path: Path) -> Path:
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
        workspace = registry_path.expanduser().resolve(strict=False).parent
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


def _validated_root_record(raw: object, *, registry_path: Path) -> Mapping[str, object]:
    if type(raw) is not dict or set(raw) != _ROOT_KEYS:
        raise _impl.CampaignEconomicAuthorityError(
            "campaign denomination witness root record is malformed"
        )
    if (
        raw["schema"] != _ROOT_SCHEMA
        or raw["schema_version"] != _ROOT_SCHEMA_VERSION
    ):
        raise _impl.CampaignEconomicAuthorityError(
            "campaign denomination witness root schema is unsupported"
        )
    if raw["registry_identity"] != _impl._registry_identity(registry_path):
        raise _impl.CampaignEconomicAuthorityError(
            "campaign denomination witness root belongs to another registry"
        )
    root = _validated_root(raw["root"], registry_path=registry_path)
    if raw["root_sha256"] != _root_sha256(str(root)):
        raise _impl.CampaignEconomicAuthorityError(
            "campaign denomination witness root digest mismatch"
        )
    return raw


def _root_record(registry) -> Mapping[str, object] | None:
    state = registry._read()
    raw = state.get(_ROOT_STATE_KEY)
    if raw is None:
        return None
    return _validated_root_record(raw, registry_path=registry.path)


def _pinned_root(registry) -> Path | None:
    record = _root_record(registry)
    if record is None:
        return None
    return _validated_root(record["root"], registry_path=registry.path)


def _campaign_atomic_write_json(path, payload: dict[str, object]) -> None:
    """Keep the durable root pin when legacy issuance publishes its stale cache image.

    ``issue_denomination_binding`` reads the registry before the first witness append.
    The append now pins the witness root durably, so the method's later cache write
    would otherwise publish its earlier pre-pin state and delete that root record.
    Merge only the already-durable, strictly validated root record into that exact
    denomination-cache publication under the existing durable path lock.
    """

    assert _ORIGINAL_CAMPAIGN_ATOMIC_WRITE_JSON is not None
    destination = Path(path)
    if (
        type(payload) is not dict
        or _impl._DENOMINATION_STATE_KEY not in payload
        or _ROOT_STATE_KEY in payload
    ):
        _ORIGINAL_CAMPAIGN_ATOMIC_WRITE_JSON(destination, payload)
        return

    with _integrity.durable_path_lock(destination):
        # Reuse the already-installed source-owned ScientificRegistry read authority
        # before trusting any durable extension bytes. This rejects duplicate keys,
        # malformed records, rollback/replay and a concurrent unsupported replacement
        # before root validation can create/touch an attacker-selected directory.
        registry = _impl.ScientificRegistry(destination)
        current = registry._read()
        durable_root = current.get(_ROOT_STATE_KEY)
        if durable_root is not None:
            _validated_root_record(durable_root, registry_path=destination)
            payload = dict(payload)
            payload[_ROOT_STATE_KEY] = durable_root
        _ORIGINAL_CAMPAIGN_ATOMIC_WRITE_JSON(destination, payload)


def _install_campaign_atomic_writer() -> None:
    if getattr(_impl, "_campaign_denomination_root_preserving_writer_installed", False):
        return
    global _ORIGINAL_CAMPAIGN_ATOMIC_WRITE_JSON
    _ORIGINAL_CAMPAIGN_ATOMIC_WRITE_JSON = _impl.atomic_write_json
    _impl.atomic_write_json = _campaign_atomic_write_json
    _impl._campaign_denomination_root_preserving_writer_installed = True


def _ensure_pinned_root(registry) -> Path:
    pinned = _pinned_root(registry)
    if pinned is not None:
        return pinned

    # issue_denomination_binding owns WorkspaceEconomicLock while the witness append
    # executes. Pin the exact root before writing the first witness so a crash can
    # leave at most a harmless root-only prefix, never an unpinned positive witness.
    try:
        selected = _impl._authority_root(registry.path)
    except Exception as exc:
        raise _impl.CampaignEconomicAuthorityError(
            "cannot select campaign denomination witness root"
        ) from exc
    selected_text = _root_text(selected)
    _validated_root(selected_text, registry_path=registry.path)

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
    _install_registry_extension_detector()
    _install_campaign_atomic_writer()
    if getattr(_impl, "_campaign_denomination_witness_root_pin_installed", False):
        return
    global _ORIGINAL_ISSUANCE_WITNESS_PATH, _ORIGINAL_APPEND_ISSUANCE_WITNESS
    _ORIGINAL_ISSUANCE_WITNESS_PATH = _impl._issuance_witness_path
    _ORIGINAL_APPEND_ISSUANCE_WITNESS = _impl._append_issuance_witness
    _impl._issuance_witness_path = _issuance_witness_path
    _impl._append_issuance_witness = _append_issuance_witness
    _impl._campaign_denomination_witness_root_pin_installed = True


_install()
