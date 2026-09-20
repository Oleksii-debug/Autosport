from __future__ import annotations

"""Authenticated durable origin proof for complete provider-board evidence.

The shared :class:`MonotonicWorkspaceAuthority` is deliberately a generic
anti-rollback primitive: callers can choose opaque state/binding digests, so its
journal alone cannot prove that provider bytes came from Autosport's fixed-origin
production acquisition path.  This guard keeps that journal as the freshness
fence and adds a machine-local HMAC issuance credential which is minted only
after ``CompleteGameBoardEvidenceStore.save`` has accepted the exact in-process
capability issued by ``capture_parlay_complete_game_board``.

A caller can therefore reproduce valid snapshot bytes and even the matching
public monotonic PREPARE/COMMIT history, but cannot make a fresh process re-issue
positive completeness authority without the authenticated production issuance
record.  Missing, deleted, truncated, or tampered issuance state fails closed.
"""

import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Final, Mapping

from . import provider_observation_authority as provider
from .integrity import atomic_write_json
from .workspace_lock import WorkspaceEconomicLock


_ISSUANCE_SCHEMA: Final = "autosport.provider-complete-game-board-issuance"
_ISSUANCE_SCHEMA_VERSION: Final = 1
_ISSUANCE_KIND: Final = "autosport-provider-complete-game-board-issuance-v1"
_KEY_BYTES: Final = 32


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _workspace_issuer_key(authority) -> str:
    material = "\0".join(
        (
            authority.workspace_instance_id,
            provider.CompleteGameBoardEvidenceStore.AUTHORITY_DOMAIN,
            _ISSUANCE_KIND,
        )
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _key_path(authority) -> Path:
    key = _workspace_issuer_key(authority)
    return (
        authority.authority_root
        / "issuer-credentials"
        / "provider-complete-game-board-v1"
        / key[:2]
        / f"{key}.key"
    )


def _record_path(authority, evidence_sha256: str) -> Path:
    key = _workspace_issuer_key(authority)
    evidence = provider._sha(evidence_sha256, "evidence_sha256")
    return (
        authority.authority_root
        / "issuer-records"
        / "provider-complete-game-board-v1"
        / key[:2]
        / key
        / f"{evidence}.json"
    )


def _secure_existing_key(path: Path) -> bytes:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise provider.ProviderObservationIntegrityError(
            "provider acquisition issuer credential is unreadable"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise provider.ProviderObservationIntegrityError(
            "provider acquisition issuer credential must be a regular file"
        )
    if os.name != "nt" and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise provider.ProviderObservationIntegrityError(
            "provider acquisition issuer credential permissions are too broad"
        )
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise provider.ProviderObservationIntegrityError(
            "provider acquisition issuer credential is unreadable"
        ) from exc
    if len(value) != _KEY_BYTES:
        raise provider.ProviderObservationIntegrityError(
            "provider acquisition issuer credential has invalid length"
        )
    return value


def _load_or_create_key(authority) -> bytes:
    path = _key_path(authority)
    if path.exists():
        return _secure_existing_key(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_bytes(_KEY_BYTES)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if descriptor is not None:
            os.close(descriptor)
        return _secure_existing_key(path)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return _secure_existing_key(path)


def _load_key(authority) -> bytes | None:
    path = _key_path(authority)
    if not path.exists():
        return None
    return _secure_existing_key(path)


def _core_payload(store, authority, snapshot) -> dict[str, object]:
    return {
        "kind": _ISSUANCE_KIND,
        "schema_version": _ISSUANCE_SCHEMA_VERSION,
        "workspace_instance_id": authority.workspace_instance_id,
        "authority_namespace_sha256": authority.namespace_sha256,
        "source_id": snapshot.request.source_id,
        "captured_at": snapshot.captured_at,
        "evidence_sha256": snapshot.evidence_sha256,
        "state_sha256": store._state_sha256(snapshot),
        "semantic_binding_sha256": store._semantic_binding_sha256(snapshot),
    }


def _issue_record(store, authority, snapshot) -> None:
    key = _load_or_create_key(authority)
    core = _core_payload(store, authority, snapshot)
    record = {
        "schema": _ISSUANCE_SCHEMA,
        **core,
        "issuer_mac_sha256": hmac.new(
            key, _canonical_bytes(core), hashlib.sha256
        ).hexdigest(),
    }
    path = _record_path(authority, snapshot.evidence_sha256)
    if path.exists():
        _verify_record(store, authority, snapshot)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, record)
    _verify_record(store, authority, snapshot)


def _verify_record(store, authority, snapshot) -> None:
    key = _load_key(authority)
    path = _record_path(authority, snapshot.evidence_sha256)
    if key is None or not path.exists():
        raise provider.ProviderObservationIntegrityError(
            "provider evidence lacks authenticated production acquisition issuance"
        )
    try:
        raw = provider.strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise provider.ProviderObservationIntegrityError(
            "provider acquisition issuance record is unreadable"
        ) from exc
    expected_fields = {
        "schema",
        "kind",
        "schema_version",
        "workspace_instance_id",
        "authority_namespace_sha256",
        "source_id",
        "captured_at",
        "evidence_sha256",
        "state_sha256",
        "semantic_binding_sha256",
        "issuer_mac_sha256",
    }
    if not isinstance(raw, dict) or set(raw) != expected_fields:
        raise provider.ProviderObservationIntegrityError(
            "provider acquisition issuance record fields mismatch"
        )
    if raw["schema"] != _ISSUANCE_SCHEMA:
        raise provider.ProviderObservationIntegrityError(
            "provider acquisition issuance schema mismatch"
        )
    core = _core_payload(store, authority, snapshot)
    for name, expected in core.items():
        if raw.get(name) != expected:
            raise provider.ProviderObservationIntegrityError(
                f"provider acquisition issuance {name} mismatch"
            )
    mac = raw.get("issuer_mac_sha256")
    if not isinstance(mac, str) or len(mac) != 64:
        raise provider.ProviderObservationIntegrityError(
            "provider acquisition issuance authentication is malformed"
        )
    expected_mac = hmac.new(key, _canonical_bytes(core), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected_mac):
        raise provider.ProviderObservationIntegrityError(
            "provider acquisition issuance authentication failed"
        )


_ORIGINAL_SAVE = provider.CompleteGameBoardEvidenceStore.save


def _save_with_authenticated_origin(self, snapshot):
    # The wrapped method first enforces the exact-object capability minted by the
    # fixed production capture path and only then publishes the provider bytes plus
    # generic rollback journal.  We mint durable origin proof strictly afterwards.
    path = _ORIGINAL_SAVE(self, snapshot)
    authority = self._authority(snapshot.evidence_sha256)
    with WorkspaceEconomicLock(self.workspace):
        # Re-resolve the exact published bytes and generic fence before issuing.
        published = self._read_path(path)
        if published.to_payload() != snapshot.to_payload():
            raise provider.ProviderObservationIntegrityError(
                "published provider evidence changed before origin issuance"
            )
        self._recover_provenance(authority, published)
        _issue_record(self, authority, published)
    return path


def _load_with_authenticated_origin(self, evidence_sha256: str):
    path = self._path(evidence_sha256)
    with WorkspaceEconomicLock(self.workspace):
        snapshot = self._read_path(path)
        if snapshot.evidence_sha256 != provider._sha(
            evidence_sha256, "evidence_sha256"
        ):
            raise provider.ProviderObservationIntegrityError(
                "content-addressed provider evidence path does not match payload"
            )
        authority = self._authority(snapshot.evidence_sha256)
        # Preserve the shared generic anti-rollback/freshness fence, then require a
        # separate non-caller-reproducible production-origin issuance fact.
        self._recover_provenance(authority, snapshot)
        _verify_record(self, authority, snapshot)
    return provider._remember(snapshot)


if not getattr(
    provider.CompleteGameBoardEvidenceStore.save,
    "_authenticated_provider_origin_guard",
    False,
):
    setattr(_save_with_authenticated_origin, "_authenticated_provider_origin_guard", True)
    provider.CompleteGameBoardEvidenceStore.save = _save_with_authenticated_origin

if not getattr(
    provider.CompleteGameBoardEvidenceStore.load,
    "_authenticated_provider_origin_guard",
    False,
):
    setattr(_load_with_authenticated_origin, "_authenticated_provider_origin_guard", True)
    provider.CompleteGameBoardEvidenceStore.load = _load_with_authenticated_origin


__all__: list[str] = []
