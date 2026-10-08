"""Bounded Betfair Basketball terminal-outcome authority adapter.

This module extends the existing canonical market_outcomes authority issuance
boundary for Betfair Basketball without creating a second outcome engine,
settlement engine, provider client, or execution authority.

The adapter consumes historical marketDefinition evidence only. Its positive
claim is limited to an exhaustive conservative WIN/LOSS/VOID terminal-state cover
for an OPEN Basketball MATCH_ODDS market whose runner roster is present in the
exact provider definition. It does not claim that every conservative state is
possible, that the evidence is live/executable, or that any economic edge exists.
"""

from __future__ import annotations

from .domain import MarketType
from .market_outcomes import (
    MarketOutcomeAuthorityAssessment,
    MarketOutcomeIdentity,
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    OutcomeRosterBasis,
    SettlementSemantics,
    _BETFAIR_MATCH_ODDS_TYPE,
    _BETFAIR_SETTLEMENT_STATUS_MAP,
    _BETFAIR_SOURCE_ID,
    _VERIFIED_AUTHORITY_TOKEN,
    _canonical_text,
    _canonical_timestamp,
    _sha256_payload,
)


_BETFAIR_BASKETBALL_EVENT_TYPE_ID = "7522"


def assess_betfair_basketball_historical_market_definition_authority(
    *,
    market_id: str,
    market_definition: dict[str, object],
    provider_publish_at: str,
    observed_at: str,
) -> MarketOutcomeAuthorityAssessment:
    """Verify one Betfair Basketball MATCH_ODDS roster from historical evidence.

    Betfair identifies Basketball with eventTypeId 7522. The adapter binds that
    provider identifier and exact MATCH_ODDS market type explicitly; display names
    are never used to infer sport or market semantics.

    Terminal outcomes use the existing conservative Cartesian WIN/LOSS/VOID
    family. This proves coverage of the canonical provider settlement alphabet
    represented by Autosport while deliberately retaining impossible combinations
    rather than pretending to have a tighter sport-specific settlement theorem.
    """

    market = _canonical_text("market_id", market_id)
    publish_raw, publish_dt = _canonical_timestamp(
        "provider_publish_at",
        provider_publish_at,
    )
    observed_raw, observed_dt = _canonical_timestamp("observed_at", observed_at)
    if publish_dt > observed_dt:
        raise ValueError("provider_publish_at must not be after observed_at")
    if type(market_definition) is not dict:
        raise ValueError("market_definition must be a JSON object")

    event_id = _canonical_text(
        "marketDefinition.eventId",
        market_definition.get("eventId"),
    )
    event_type_id = _canonical_text(
        "marketDefinition.eventTypeId",
        market_definition.get("eventTypeId"),
    )
    provider_market_type = _canonical_text(
        "marketDefinition.marketType",
        market_definition.get("marketType"),
    )
    status = _canonical_text(
        "marketDefinition.status",
        market_definition.get("status"),
    ).upper()

    identity = MarketOutcomeIdentity(
        sport="basketball",
        event_id=event_id,
        market_id=market,
        source_id=_BETFAIR_SOURCE_ID,
        market_type=(
            MarketType.WINNER
            if provider_market_type == _BETFAIR_MATCH_ODDS_TYPE
            else MarketType.OTHER
        ),
    )
    if event_type_id != _BETFAIR_BASKETBALL_EVENT_TYPE_ID:
        return MarketOutcomeAuthorityAssessment(
            identity=identity,
            status=OutcomeAuthorityStatus.REFUSED,
            authority=None,
            refusal_reason=(
                "betfair_event_type_has_no_verified_basketball_roster_protocol"
            ),
        )
    if provider_market_type != _BETFAIR_MATCH_ODDS_TYPE:
        return MarketOutcomeAuthorityAssessment(
            identity=identity,
            status=OutcomeAuthorityStatus.REFUSED,
            authority=None,
            refusal_reason=(
                "market_type_has_no_supported_terminal_settlement_semantics"
            ),
        )
    if status != "OPEN":
        return MarketOutcomeAuthorityAssessment(
            identity=identity,
            status=OutcomeAuthorityStatus.REFUSED,
            authority=None,
            refusal_reason="betfair_market_definition_is_not_open_at_roster_revision",
        )

    runners = market_definition.get("runners")
    if type(runners) is not list or len(runners) < 2:
        return MarketOutcomeAuthorityAssessment(
            identity=identity,
            status=OutcomeAuthorityStatus.REFUSED,
            authority=None,
            refusal_reason=(
                "betfair_market_definition_lacks_authoritative_runner_roster"
            ),
        )

    selection_ids: list[str] = []
    for index, runner in enumerate(runners):
        if type(runner) is not dict or runner.get("id") is None:
            raise ValueError(f"marketDefinition.runners[{index}] requires id")
        raw_selection_id = runner["id"]
        if isinstance(raw_selection_id, bool) or not isinstance(
            raw_selection_id,
            (str, int),
        ):
            raise ValueError(
                f"marketDefinition.runners[{index}].id must be string or integer"
            )
        selection_ids.append(
            _canonical_text(
                f"marketDefinition.runners[{index}].id",
                str(raw_selection_id),
            )
        )
    if len(selection_ids) != len(set(selection_ids)):
        raise ValueError("marketDefinition.runners contains duplicate selection id")
    canonical_selections = tuple(sorted(selection_ids))

    definition_payload = {
        "provider": _BETFAIR_SOURCE_ID,
        "provider_publish_at": publish_raw,
        "market_id": market,
        "market_definition": market_definition,
    }
    roster_provenance_sha256 = _sha256_payload(definition_payload)

    settlement_protocol = {
        "provider": _BETFAIR_SOURCE_ID,
        "sport": "basketball",
        "event_type_id": _BETFAIR_BASKETBALL_EVENT_TYPE_ID,
        "provider_market_type": _BETFAIR_MATCH_ODDS_TYPE,
        "canonical_status_map": dict(
            sorted(_BETFAIR_SETTLEMENT_STATUS_MAP.items())
        ),
        "terminal_family": (
            SettlementSemantics.CANONICAL_WIN_LOSS_VOID_SUPERSET.value
        ),
        "terminal_space_exact": False,
    }
    settlement_rules_sha256 = _sha256_payload(settlement_protocol)
    verification_protocol_sha256 = _sha256_payload(
        {
            "protocol": (
                "autosport.betfair_historical.basketball.market_definition_roster.v1"
            ),
            "sport": "basketball",
            "event_type_id": _BETFAIR_BASKETBALL_EVENT_TYPE_ID,
            "market_type": _BETFAIR_MATCH_ODDS_TYPE,
            "requires_open_status": True,
            "runner_ids_derived_from": "marketDefinition.runners",
            "settlement_protocol_sha256": settlement_rules_sha256,
        }
    )
    source_revision = (
        "betfair-basketball-market-definition:"
        + publish_raw
        + ":"
        + roster_provenance_sha256[:16]
    )

    authority = MarketSettlementOutcomeAuthority(
        identity=identity,
        selection_ids=canonical_selections,
        roster_basis=OutcomeRosterBasis.PROVIDER_MARKET_DEFINITION,
        settlement_semantics=(
            SettlementSemantics.CANONICAL_WIN_LOSS_VOID_SUPERSET
        ),
        source_revision=source_revision,
        causal_cutoff=publish_raw,
        observed_at=observed_raw,
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
