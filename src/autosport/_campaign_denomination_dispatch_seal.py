from __future__ import annotations

"""Fail closed if campaign-denomination re-resolution dispatch is replaced.

The campaign economics reader already pins ``denomination_binding`` itself, but that
method calls a small graph of other class methods. Pin the complete transitive
``FinalizedCampaignAuthority`` read graph after product composition is loaded so a
same-process class monkeypatch cannot synthesize positive denomination authority
from missing durable state or bypass the monotonic issuance witness.
"""

# Freeze the independent denomination-witness location before snapshotting the
# authority read graph. This keeps supported environment changes from redirecting an
# already-issued campaign onto a fresh witness ancestry after restart/cache loss.
from . import _campaign_denomination_witness_root_pin as _witness_root_pin  # noqa: F401,E402
from . import campaign_cost_evidence as _cost_evidence
from .campaign_economic_authority import FinalizedCampaignAuthority as _Authority


_GRAPH_METHOD_NAMES = (
    "projection",
    "_binding_key",
    "_registry",
    "_binding_source",
    "_derive_binding",
    "_persisted_payload",
    "_issuance_witness",
    "_verified_persisted_binding",
    "denomination_binding",
)
_AUTHORITY_GRAPH = tuple(
    (name, _Authority.__dict__[name]) for name in _GRAPH_METHOD_NAMES
)
_PRODUCT_READER = _cost_evidence._PRODUCT_DENOMINATION_READER


def _graph_is_exact() -> bool:
    return all(
        _Authority.__dict__.get(name) is expected
        for name, expected in _AUTHORITY_GRAPH
    )


def _sealed_product_denomination_reader(campaign: _Authority):
    # Dispatch drift is absence of authority, not an alternate authority source.
    # Return None so campaign economics preserves MISSING_CAMPAIGN_CURRENCY_AUTHORITY.
    if type(campaign) is not _Authority or not _graph_is_exact():
        return None
    resolved = _PRODUCT_READER(campaign)
    # Recheck after the read so a mutation concurrent with ordinary resolution
    # cannot be returned as positive authority merely because the entry check passed.
    if not _graph_is_exact():
        return None
    return resolved


_cost_evidence._PRODUCT_DENOMINATION_READER = _sealed_product_denomination_reader

# Install only after FinalizedCampaignAuthority is fully composed and sealed.  The
# lifecycle layer stages this exact authority on a draft clone before exposing the
# irreversible PaperCampaign finalized transition.
from . import _campaign_finalize_atomicity as _campaign_finalize_atomicity  # noqa: E402,F401
