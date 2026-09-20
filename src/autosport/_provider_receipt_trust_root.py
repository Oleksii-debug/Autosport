from __future__ import annotations

"""Keep provider-origin receipts on the production machine trust root.

``CompleteGameBoardEvidenceStore.authority_root`` remains useful for exercising the
shared generic monotonic authority in isolated tests, but it is caller supplied and
therefore cannot also select the credential root that proves *provider acquisition
origin*.  Otherwise a caller can point the store at a directory it controls, seed a
chosen receipt key plus HMAC, and turn self-authored bytes into positive completeness.

The durable provider receipt is therefore always resolved through Autosport's
production machine-state root configuration.  A caller-selected monotonic root can
prove continuity of caller-chosen state, but never production acquisition origin.
The machine-local receipt credential is also required to be a private regular file;
symlink/substitution and broad POSIX permissions fail closed.
"""

import os
import secrets
import stat
from pathlib import Path

from . import provider_observation_authority as provider
from .monotonic_workspace_authority import (
    MonotonicWorkspaceAuthorityError,
    resolve_monotonic_authority_root,
)


def _production_receipt_root(self):
    try:
        root = resolve_monotonic_authority_root(self.workspace, None)
    except MonotonicWorkspaceAuthorityError as exc:
        raise provider.ProviderObservationIntegrityError(
            "provider acquisition receipt trust root is unsafe"
        ) from exc
    return root / provider._RECEIPT_ROOT_NAME / self._workspace_sha256()


def _secure_existing_receipt_key(path: Path) -> bytes:
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


def _secure_read_receipt_key(self, *, create: bool) -> bytes:
    path = self._key_path()
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
    return _secure_existing_receipt_key(path)


if not getattr(
    provider.CompleteGameBoardEvidenceStore._receipt_root,
    "_production_provider_receipt_root_guard",
    False,
):
    setattr(
        _production_receipt_root,
        "_production_provider_receipt_root_guard",
        True,
    )
    provider.CompleteGameBoardEvidenceStore._receipt_root = _production_receipt_root

if not getattr(
    provider.CompleteGameBoardEvidenceStore._read_receipt_key,
    "_secure_provider_receipt_key_guard",
    False,
):
    setattr(_secure_read_receipt_key, "_secure_provider_receipt_key_guard", True)
    provider.CompleteGameBoardEvidenceStore._read_receipt_key = _secure_read_receipt_key


__all__: list[str] = []
