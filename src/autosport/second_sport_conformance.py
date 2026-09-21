from __future__ import annotations

"""Bounded second-sport engineering-conformance adapter.

This module intentionally carries *test-only* evidence.  It exists to exercise the
canonical sport-generic outcome/settlement/portfolio path with semantics that differ
from the current table-tennis provider path without pretending that Autosport has a
lawful external soccer feed, provider entitlement, or economic qualification.

The closed fixture is deliberately not a generic caller-authority escape hatch:
only one fixed, versioned rule is recognized and every resulting authority is bound
to the explicit ``test_only_engineering_conformance`` source identity.
"""

from .domain import MarketType
from .market_outcomes import (
    MarketOutcomeAuthorityAssessment,
    MarketOutcomeIdentity,
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    OutcomeRosterBasis,
    SettlementSemantics,
    _VERIFIED_AUTHORITY_TOKEN,
    _sha256_payload,
)


TEST_ONLY_SECOND_SPORT_SOURCE_ID = "test_only_engineering_conformance"
TEST_ONLY_SECOND_SPORT_RULE_ID = "test-only.soccer.match-result-3way.v1"
_TEST_ONLY_SECOND_SPORT_SELECTION_IDS = ("away", "draw", "home")
_TEST_ONLY_SECOND_SPORT_PROTOCOL = (
    "autosport.test_only.second_sport_conformance.soccer_match_result_3way.v1"
)


def assess_test_only_second_sport_market_authority(
    *,
    event_id: str,
    market_id: str,
    market_rule_id: str,
    causal_cutoff: str,
    observed_at: str,
) -> MarketOutcomeAuthorityAssessment:
    """Return authority only for the closed deterministic engineering fixture.

    The fixture models a three-way match-result market where exactly one of
    away/draw/home wins, plus an all-void terminal state.  This is intentionally
    different from the Betfair table-tennis adapter's conservative Cartesian
    win/loss/void cover.  Passing another rule id fails closed.

    A successful result proves only sport-generic engineering conformance.  The
    source identity and verification hashes permanently label it as TEST_ONLY;
    callers must not treat it as external/provider/economic evidence.
    """

    identity = MarketOutcomeIdentity(
        sport="soccer",
        event_id=event_id,
        market_id=market_id,
        source_id=TEST_ONLY_SECOND_SPORT_SOURCE_ID,
        market_type=MarketType.WINNER,
    )
    if market_rule_id != TEST_ONLY_SECOND_SPORT_RULE_ID:
        return MarketOutcomeAuthorityAssessment(
            identity=identity,
            status=OutcomeAuthorityStatus.REFUSED,
            authority=None,
            refusal_reason="test_only_second_sport_market_rule_unsupported",
        )

    fixture_definition = {
        "evidence_grade": "TEST_ONLY_ENGINEERING_CONFORMANCE",
        "external_provider_evidence": False,
        "sport": identity.sport,
        "event_id": identity.event_id,
        "market_id": identity.market_id,
        "market_rule_id": TEST_ONLY_SECOND_SPORT_RULE_ID,
        "selection_ids": list(_TEST_ONLY_SECOND_SPORT_SELECTION_IDS),
        "causal_cutoff": causal_cutoff,
    }
    roster_provenance_sha256 = _sha256_payload(fixture_definition)
    settlement_protocol = {
        "evidence_grade": "TEST_ONLY_ENGINEERING_CONFORMANCE",
        "sport": identity.sport,
        "market_rule_id": TEST_ONLY_SECOND_SPORT_RULE_ID,
        "terminal_family": SettlementSemantics.EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID.value,
        "selection_ids": list(_TEST_ONLY_SECOND_SPORT_SELECTION_IDS),
        "terminal_space_exact": True,
    }
    settlement_rules_sha256 = _sha256_payload(settlement_protocol)
    verification_protocol_sha256 = _sha256_payload(
        {
            "protocol": _TEST_ONLY_SECOND_SPORT_PROTOCOL,
            "source_id": TEST_ONLY_SECOND_SPORT_SOURCE_ID,
            "external_provider_evidence": False,
            "market_rule_id": TEST_ONLY_SECOND_SPORT_RULE_ID,
            "settlement_protocol_sha256": settlement_rules_sha256,
        }
    )
    source_revision = (
        "test-only-second-sport:"
        + TEST_ONLY_SECOND_SPORT_RULE_ID
        + ":"
        + roster_provenance_sha256[:16]
    )
    authority = MarketSettlementOutcomeAuthority(
        identity=identity,
        selection_ids=_TEST_ONLY_SECOND_SPORT_SELECTION_IDS,
        roster_basis=OutcomeRosterBasis.GOVERNED_DATASET_MARKET_DEFINITION,
        settlement_semantics=SettlementSemantics.EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID,
        source_revision=source_revision,
        causal_cutoff=causal_cutoff,
        observed_at=observed_at,
        roster_provenance_sha256=roster_provenance_sha256,
        settlement_rules_sha256=settlement_rules_sha256,
        verification_protocol_sha256=verification_protocol_sha256,
        _verification_token=_VERIFIED_AUTHORITY_TOKEN,
    )
    return MarketOutcomeAuthorityAssessment(
        identity=identity,
        status=OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        authority=authority,
        refusal_reason=None,
    )
