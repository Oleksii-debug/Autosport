from __future__ import annotations

"""Seal durable provider-origin receipt issuance behind canonical capture authority.

The generic monotonic authority is caller-constructible and remains only a rollback
fence. Provider origin is a separate trust decision: a durable receipt may be minted
only while the exact in-process snapshot capability issued by the fixed production
acquisition path is live.

The receipt verifier remains restart-capable, but the consumer-facing evidence store
no longer returns the machine signing key, exposes a generic receipt signer, or
exposes the credential/receipt trust-root paths. Credential IO and signing live in
an installation-local closure used only by the capability-gated writer and the
restart verifier.

Provider-origin credentials intentionally do *not* use
``AUTOSPORT_MONOTONIC_AUTHORITY_ROOT`` (nor a constructor ``authority_root``). Those
selectors exist for the generic rollback journal and are therefore caller-controlled
by design. The provider receipt instead uses an Autosport-owned per-user application
state location derived from the OS account home and rejects any workspace that would
contain, or be contained by, that trust root.
"""

import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path
from typing import Mapping

from . import provider_observation_authority as provider
from .integrity import atomic_write_json


def _install_guard() -> None:
    store_type = provider.CompleteGameBoardEvidenceStore
    if getattr(store_type._write_receipt, "_sealed_provider_receipt_issuer", False):
        return

    def receipt_root(store) -> Path:
        # Do not call default/resolve_monotonic_authority_root here. In particular,
        # AUTOSPORT_MONOTONIC_AUTHORITY_ROOT is an intentional caller/test seam for
        # the generic journal and therefore cannot select provider-origin trust.
        try:
            home = Path.home().resolve(strict=False)
            workspace = store.workspace.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise provider.ProviderObservationIntegrityError(
                "provider acquisition receipt trust root is unsafe"
            ) from exc
        if not home.is_absolute() or not workspace.is_absolute():
            raise provider.ProviderObservationIntegrityError(
                "provider acquisition receipt trust root is unsafe"
            )

        if os.name == "nt":
            base = (
                home
                / "AppData"
                / "Local"
                / "Autosport"
                / "application-state"
                / "provider-origin-receipt-v1"
            )
        else:
            base = home / ".local" / "state" / "autosport" / "provider-origin-receipt-v1"
        try:
            root = base.resolve(strict=False)
        except OSError as exc:
            raise provider.ProviderObservationIntegrityError(
                "provider acquisition receipt trust root is unsafe"
            ) from exc
        if (
            root == workspace
            or root.is_relative_to(workspace)
            or workspace.is_relative_to(root)
        ):
            raise provider.ProviderObservationIntegrityError(
                "provider acquisition receipt trust root must be disjoint from workspace"
            )
        if root.exists() and not root.is_dir():
            raise provider.ProviderObservationIntegrityError(
                "provider acquisition receipt trust root must be a directory"
            )
        return root / store._workspace_sha256()

    def key_path(store) -> Path:
        return receipt_root(store) / "receipt.key"

    def receipt_path(store, evidence_sha256: str) -> Path:
        digest = provider._sha(evidence_sha256, "evidence_sha256")
        return receipt_root(store) / "receipts" / f"{digest}.json"

    def unsigned_receipt(store, snapshot) -> dict[str, object]:
        return {
            "schema": provider._RECEIPT_SCHEMA,
            "schema_version": provider._RECEIPT_SCHEMA_VERSION,
            "workspace_sha256": store._workspace_sha256(),
            "evidence_sha256": snapshot.evidence_sha256,
            "state_sha256": store._state_sha256(snapshot),
            "semantic_binding_sha256": store._semantic_binding_sha256(snapshot),
        }

    def receipt_hmac(key: bytes, payload: Mapping[str, object]) -> str:
        return hmac.new(
            key,
            provider._canonical_json(dict(payload)).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def secure_existing_key(path: Path) -> bytes:
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise provider.ProviderObservationIntegrityError(
                "provider evidence is not proven by production-owned acquisition receipt"
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            raise provider.ProviderObservationIntegrityError(
                "provider acquisition receipt key must be a regular file"
            )
        if os.name != "nt" and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise provider.ProviderObservationIntegrityError(
                "provider acquisition receipt key permissions are too broad"
            )
        try:
            raw = path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise provider.ProviderObservationIntegrityError(
                "provider evidence is not proven by production-owned acquisition receipt"
            ) from exc
        if len(raw) != provider._RECEIPT_KEY_BYTES * 2 or any(
            character not in provider._HEX for character in raw
        ):
            raise provider.ProviderObservationIntegrityError(
                "provider acquisition receipt key is malformed"
            )
        key = bytes.fromhex(raw)
        if len(key) != provider._RECEIPT_KEY_BYTES:
            raise provider.ProviderObservationIntegrityError(
                "provider acquisition receipt key is malformed"
            )
        return key

    def load_key(store, *, create: bool) -> bytes:
        path = key_path(store)
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            value = secrets.token_bytes(provider._RECEIPT_KEY_BYTES)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            if os.name != "nt":
                flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor: int | None = None
            created = False
            try:
                descriptor = os.open(path, flags, 0o600)
                created = True
                with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                    descriptor = None
                    handle.write(value.hex())
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                if descriptor is not None:
                    os.close(descriptor)
            except BaseException:
                if descriptor is not None:
                    os.close(descriptor)
                if created:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                raise
        return secure_existing_key(path)

    def write_receipt(store, snapshot) -> None:
        # This exact-object capability can be obtained only from the fixed canonical
        # acquisition path (or from a prior receipt+journal verified restart load).
        # save() calls this writer only after asserting the same capability.
        provider.assert_complete_game_board_authoritative(snapshot)
        unsigned = unsigned_receipt(store, snapshot)
        key = load_key(store, create=True)
        receipt = dict(unsigned)
        receipt["hmac_sha256"] = receipt_hmac(key, unsigned)
        path = receipt_path(store, snapshot.evidence_sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, receipt)

    def verify_receipt(store, snapshot) -> None:
        path = receipt_path(store, snapshot.evidence_sha256)
        try:
            raw = provider.strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise provider.ProviderObservationIntegrityError(
                "provider evidence is not proven by production-owned acquisition receipt"
            ) from exc
        if not isinstance(raw, dict) or set(raw) != provider._RECEIPT_KEYS:
            raise provider.ProviderObservationIntegrityError(
                "provider acquisition receipt is malformed"
            )
        expected_unsigned = unsigned_receipt(store, snapshot)
        for name, expected in expected_unsigned.items():
            if raw.get(name) != expected:
                raise provider.ProviderObservationIntegrityError(
                    "provider acquisition receipt does not bind exact persisted evidence"
                )
        supplied_hmac = provider._sha(raw.get("hmac_sha256"), "hmac_sha256")
        key = load_key(store, create=False)
        expected_hmac = receipt_hmac(key, expected_unsigned)
        if not hmac.compare_digest(supplied_hmac, expected_hmac):
            raise provider.ProviderObservationIntegrityError(
                "provider acquisition receipt authentication failed"
            )

    def deny_consumer_credential_access(*args, **kwargs):
        del args, kwargs
        raise provider.ProviderObservationUnsupportedError(
            "provider receipt signing material is not a consumer API"
        )

    setattr(write_receipt, "_sealed_provider_receipt_issuer", True)
    setattr(verify_receipt, "_sealed_provider_receipt_verifier", True)

    # The only positive signing operation is the exact-object capability-gated
    # writer above. These legacy helpers previously exposed enough material to
    # manufacture a production-root receipt for caller-created bytes.
    store_type._write_receipt = write_receipt
    store_type._verify_receipt = verify_receipt
    store_type._receipt_root = deny_consumer_credential_access
    store_type._receipt_path = deny_consumer_credential_access
    store_type._key_path = deny_consumer_credential_access
    store_type._read_receipt_key = deny_consumer_credential_access
    store_type._unsigned_receipt = deny_consumer_credential_access
    store_type._receipt_hmac = staticmethod(deny_consumer_credential_access)


_install_guard()
del _install_guard

__all__: list[str] = []
