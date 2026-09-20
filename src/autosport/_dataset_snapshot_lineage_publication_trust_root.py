from __future__ import annotations

"""Fence legacy lineage publication origin to Autosport's production machine root.

The generic DatasetSnapshot lineage authority intentionally accepts an injected
``authority_root``/workspace identity for deterministic rollback testing.  Those
inputs are caller controlled and therefore cannot also select the trust root that
proves a causal legacy-proof re-observation.

This guard keeps the generic monotonic authority as continuity/rollback evidence,
but moves publication issuance credentials and records to the product machine-state
root resolved without caller injection.  More importantly, a lineage instance whose
generic monotonic root is not that production root is structurally/test-only: it may
persist witness-shaped audit bytes but can never mint positive causal publication
authority.
"""

from pathlib import Path

from . import _dataset_snapshot_lineage_publication as publication
from . import _dataset_snapshot_lineage_publication_provenance as provenance
from .monotonic_workspace_authority import (
    MonotonicWorkspaceAuthorityError,
    resolve_monotonic_authority_root,
)


_CREDENTIAL_ROOT = "dataset-lineage-publication-origin-v1"


def _production_root(
    authority: publication.LegacyLineagePublicationAuthority,
) -> Path:
    try:
        return resolve_monotonic_authority_root(
            authority.path.parent.resolve(strict=False),
            None,
        )
    except MonotonicWorkspaceAuthorityError as exc:
        raise ValueError("legacy publication production trust root is unsafe") from exc


def _is_production_lineage(
    authority: publication.LegacyLineagePublicationAuthority,
) -> bool:
    try:
        actual = authority.lineage.monotonic_authority.authority_root.resolve(
            strict=False
        )
        expected = _production_root(authority).resolve(strict=False)
    except OSError as exc:
        raise ValueError("legacy publication trust roots cannot be resolved") from exc
    return actual == expected


def _production_key_path(
    authority: publication.LegacyLineagePublicationAuthority,
) -> Path:
    workspace_key = provenance._workspace_key(authority)
    return (
        _production_root(authority)
        / _CREDENTIAL_ROOT
        / "issuer-credentials"
        / workspace_key[:2]
        / f"{workspace_key}.key"
    )


def _production_issuance_path(
    authority: publication.LegacyLineagePublicationAuthority,
) -> Path:
    workspace_key = provenance._workspace_key(authority)
    return (
        _production_root(authority)
        / _CREDENTIAL_ROOT
        / "issuer-records"
        / workspace_key[:2]
        / f"{workspace_key}.json"
    )


_AUTHENTICATED_PUBLISH = publication.LegacyLineagePublicationAuthority.publish_exact_chain
_AUTHENTICATED_WITNESS_FOR = publication.LegacyLineagePublicationAuthority.witness_for


def _publish_exact_chain_production_only(
    self: publication.LegacyLineagePublicationAuthority,
    records,
):
    if not _is_production_lineage(self):
        # Structural/test roots may retain deterministic witness bytes for rollback
        # exercises, but they deliberately receive no authenticated issuance record.
        return provenance._ORIGINAL_PUBLISH_EXACT_CHAIN(self, records)
    return _AUTHENTICATED_PUBLISH(self, records)


def _witness_for_production_only(
    self: publication.LegacyLineagePublicationAuthority,
    record,
):
    if not _is_production_lineage(self):
        return None
    return _AUTHENTICATED_WITNESS_FOR(self, record)


# The existing provenance layer resolves these helpers dynamically, so replacing the
# paths here moves both creation and verification away from caller-selected generic
# monotonic roots without creating a second publication authority.
provenance._key_path = _production_key_path
provenance._issuance_path = _production_issuance_path

if not getattr(
    publication.LegacyLineagePublicationAuthority.publish_exact_chain,
    "_production_trust_root_guard",
    False,
):
    setattr(
        _publish_exact_chain_production_only,
        "_production_trust_root_guard",
        True,
    )
    publication.LegacyLineagePublicationAuthority.publish_exact_chain = (
        _publish_exact_chain_production_only
    )

if not getattr(
    publication.LegacyLineagePublicationAuthority.witness_for,
    "_production_trust_root_guard",
    False,
):
    setattr(
        _witness_for_production_only,
        "_production_trust_root_guard",
        True,
    )
    publication.LegacyLineagePublicationAuthority.witness_for = (
        _witness_for_production_only
    )


__all__: list[str] = []
