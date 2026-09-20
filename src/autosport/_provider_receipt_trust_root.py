from __future__ import annotations

"""Seal complete-board provider authority behind canonical acquisition boundaries.

The generic monotonic authority is caller-constructible and remains only a rollback
fence. Provider origin is a separate trust decision: an in-process capability is
minted only by the fixed production capture path or after restart verification of an
already-authenticated durable receipt plus monotonic history. Durable receipt signing
is then gated by that exact-object capability.

Consumer code cannot mutate the capability registry, call the former ``_remember``
issuer, obtain receipt signing material, invoke a generic receipt signer, or select
the provider credential root. The public/test-configurable generic monotonic root
continues to fence rollback only.
"""

import hashlib
import hmac
import os
import secrets
import stat
import weakref
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from . import provider_observation_authority as provider
from .integrity import atomic_write_json


def _install_guard() -> None:
    store_type = provider.CompleteGameBoardEvidenceStore
    if getattr(store_type._write_receipt, "_sealed_provider_receipt_issuer", False):
        return

    # ------------------------------------------------------------------
    # Ephemeral exact-object authority.
    #
    # The predecessor kept both its issuer (_remember) and mutable registry
    # (_ISSUED) in module globals. Either was enough for ordinary consumer code to
    # manufacture an in-process capability for a caller-created snapshot. Keep the
    # mutable registry and issuer only in this lexical scope; expose at most a
    # read-only diagnostic view of membership.
    # ------------------------------------------------------------------
    issued: dict[int, tuple[weakref.ReferenceType, str]] = {}

    def forget_issued(snapshot_id: int, reference: weakref.ReferenceType) -> None:
        current = issued.get(snapshot_id)
        if current is not None and current[0] is reference:
            issued.pop(snapshot_id, None)

    def issue_snapshot(snapshot: provider.CompleteGameBoardSnapshot):
        snapshot_id = id(snapshot)
        reference = weakref.ref(
            snapshot,
            lambda current, snapshot_id=snapshot_id: forget_issued(snapshot_id, current),
        )
        issued[snapshot_id] = (reference, snapshot.evidence_sha256)
        return snapshot

    def assert_authoritative(snapshot: provider.CompleteGameBoardSnapshot) -> None:
        if not isinstance(snapshot, provider.CompleteGameBoardSnapshot):
            raise provider.ProviderObservationUnsupportedError(
                "complete provider authority requires CompleteGameBoardSnapshot"
            )
        current = issued.get(id(snapshot))
        if (
            current is None
            or current[0]() is not snapshot
            or current[1] != snapshot.evidence_sha256
        ):
            raise provider.ProviderObservationUnsupportedError(
                "snapshot was not issued by canonical provider acquisition evidence"
            )

    def deny_direct_issuance(*args, **kwargs):
        del args, kwargs
        raise provider.ProviderObservationUnsupportedError(
            "provider authority issuance is not a consumer API"
        )

    def capture_parlay_complete_game_board(
        *,
        api_key: str,
        request: provider.CompleteGameBoardRequest,
        timeout_seconds: float = 10.0,
    ) -> provider.CompleteGameBoardSnapshot:
        """Acquire and issue only through the fixed production Parlay boundary."""

        if not isinstance(api_key, str) or not api_key or api_key != api_key.strip():
            raise ValueError("api_key must be non-empty trimmed text")
        if any(character.isspace() for character in api_key):
            raise ValueError("api_key must not contain whitespace")
        if not isinstance(request, provider.CompleteGameBoardRequest):
            raise TypeError("request must be CompleteGameBoardRequest")
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, (int, float)
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        timeout = float(timeout_seconds)
        if not provider.math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        frame = provider._read_production_initial_state(
            request,
            api_key=api_key,
            timeout_seconds=timeout,
        )
        snapshot = provider.CompleteGameBoardSnapshot(
            request=request,
            captured_at=provider._default_clock(),
            frame_json=provider._canonical_json(dict(frame)),
        )
        return issue_snapshot(snapshot)

    # ------------------------------------------------------------------
    # Durable provider-origin receipt.
    # ------------------------------------------------------------------
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
        assert_authoritative(snapshot)
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

    def load_authoritative(store, evidence_sha256: str):
        """Re-issue only after durable receipt and rollback-fence verification."""

        path = store._path(evidence_sha256)
        with provider.WorkspaceEconomicLock(store.workspace):
            snapshot = store._read_path(path)
            if snapshot.evidence_sha256 != provider._sha(
                evidence_sha256, "evidence_sha256"
            ):
                raise provider.ProviderObservationIntegrityError(
                    "content-addressed provider evidence path does not match payload"
                )
            verify_receipt(store, snapshot)
            authority = store._authority(snapshot.evidence_sha256)
            store._recover_provenance(authority, snapshot)
        return issue_snapshot(snapshot)

    def deny_consumer_credential_access(*args, **kwargs):
        del args, kwargs
        raise provider.ProviderObservationUnsupportedError(
            "provider receipt signing material is not a consumer API"
        )

    setattr(write_receipt, "_sealed_provider_receipt_issuer", True)
    setattr(verify_receipt, "_sealed_provider_receipt_verifier", True)
    setattr(capture_parlay_complete_game_board, "_sealed_provider_capture_issuer", True)
    setattr(load_authoritative, "_sealed_provider_restart_issuer", True)

    # Replace every ordinary consumer issuance/signing primitive. The methods that
    # remain callable either perform the fixed production network acquisition or
    # first verify the durable origin receipt plus generic rollback history.
    provider._ISSUED = MappingProxyType(issued)
    provider._remember = deny_direct_issuance
    provider.assert_complete_game_board_authoritative = assert_authoritative
    provider.capture_parlay_complete_game_board = capture_parlay_complete_game_board
    store_type.load = load_authoritative
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
