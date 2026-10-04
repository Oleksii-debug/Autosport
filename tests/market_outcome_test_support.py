"""Test-only issuance of governed synthetic terminal-outcome authority.

Production adapters must never use this module.  Tests that exercise downstream
settlement/science logic need a real integrity-registered authority without
misrepresenting caller-authored Betfair marketDefinition bytes as provider evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from autosport import market_outcomes as _market_outcomes
from autosport.domain import MarketType


def _digest(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def issue_synthetic_market_outcome_authority(
    *,
    event_id: str,
    market_id: str,
    selection_ids: Iterable[str],
    causal_cutoff: str,
    observed_at: str,
    source_id: str = "betfair_exchange_historical",
    sport: str = "table_tennis",
) -> _market_outcomes.MarketSettlementOutcomeAuthority:
    """Issue one explicitly synthetic, governed test authority.

    The private issuance context is deliberately confined to tests.  The resulting
    object still passes the production exact-type, class-dispatch and hidden
    identity/digest registry checks used by downstream consumers.
    """

    selections = tuple(sorted(selection_ids))
    identity = _market_outcomes.MarketOutcomeIdentity(
        sport=sport,
        event_id=event_id,
        market_id=market_id,
        source_id=source_id,
        market_type=MarketType.WINNER,
    )
    roster_sha = _digest(
        {
            "kind": "autosport.test-governed-outcome-roster.v1",
            "identity": identity.to_dict(),
            "selection_ids": list(selections),
            "causal_cutoff": causal_cutoff,
            "observed_at": observed_at,
        }
    )
    rules_sha = _digest(
        {
            "kind": "autosport.test-canonical-win-loss-void-superset.v1",
            "settlement_semantics": (
                _market_outcomes.SettlementSemantics.CANONICAL_WIN_LOSS_VOID_SUPERSET.value
            ),
        }
    )
    protocol_sha = _digest(
        {
            "kind": "autosport.test-governed-market-outcome-authority.v1",
            "roster_sha256": roster_sha,
            "settlement_rules_sha256": rules_sha,
        }
    )
    issuance = _market_outcomes._VERIFIED_AUTHORITY_ISSUANCE.set(True)
    try:
        authority = _market_outcomes.MarketSettlementOutcomeAuthority(
            identity=identity,
            selection_ids=selections,
            roster_basis=(
                _market_outcomes.OutcomeRosterBasis.GOVERNED_DATASET_MARKET_DEFINITION
            ),
            settlement_semantics=(
                _market_outcomes.SettlementSemantics.CANONICAL_WIN_LOSS_VOID_SUPERSET
            ),
            source_revision=f"test-governed-roster:{roster_sha[:16]}",
            causal_cutoff=causal_cutoff,
            observed_at=observed_at,
            roster_provenance_sha256=roster_sha,
            settlement_rules_sha256=rules_sha,
            verification_protocol_sha256=protocol_sha,
            _verification_token=_market_outcomes._VERIFIED_AUTHORITY_TOKEN,
        )
    finally:
        _market_outcomes._VERIFIED_AUTHORITY_ISSUANCE.reset(issuance)
    authority.assert_issued_integrity()
    return authority
