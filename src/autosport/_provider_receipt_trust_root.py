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
"""

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


__all__: list[str] = []
