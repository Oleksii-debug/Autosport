from autosport.market_outcomes import (
    OutcomeAuthorityStatus,
    assess_betfair_historical_market_definition_authority,
)


def test_caller_constructed_market_definition_cannot_mint_provider_outcome_authority():
    """Raw caller bytes/timestamps are not proof of Betfair provider origin."""

    fabricated_definition = {
        "eventId": "caller-invented-event",
        "eventTypeId": "2593174",
        "marketType": "MATCH_ODDS",
        "status": "OPEN",
        "runners": [{"id": "101"}, {"id": "202"}],
    }

    try:
        assessment = assess_betfair_historical_market_definition_authority(
            market_id="caller-invented-market",
            market_definition=fabricated_definition,
            provider_publish_at="2026-09-22T10:00:00Z",
            observed_at="2026-09-22T10:00:01Z",
        )
    except (TypeError, ValueError):
        # A future repair may require a separately verified acquisition/origin
        # witness in the function signature or fail before assessment issuance.
        return

    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert assessment.authority is None
    assert assessment.refusal_reason is not None
