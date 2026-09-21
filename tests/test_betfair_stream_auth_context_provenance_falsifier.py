from __future__ import annotations

from decimal import Decimal

from autosport.betfair_stream_codec import (
    BETFAIR_STREAM_SOURCE_ID,
    BetfairQuoteIdentity,
    BetfairQuoteSide,
)
from autosport.betfair_stream_publish_freshness import (
    BetfairStreamFreshnessPolicy,
    BetfairStreamPublishFreshnessRuntime,
    BetfairStreamSubscriptionContext,
)


def test_caller_chosen_auth_context_and_raw_frame_cannot_mint_decision_eligible_freshness() -> None:
    """Caller strings/raw dictionaries are not authenticated provider authority."""

    try:
        context = BetfairStreamSubscriptionContext(
            authenticated_context_id="caller-picked-auth-context",
            subscription_id="caller-picked-subscription",
            subscription_generation=1,
            criteria_sha256="a" * 64,
            heartbeat_ms=5000,
            conflate_ms=0,
        )
        runtime = BetfairStreamPublishFreshnessRuntime(context)
        runtime.ingest_raw(
            {
                "op": "mcm",
                "ct": "SUB_IMAGE",
                "initialClk": "caller-initial",
                "clk": "caller-clk",
                "pt": 1000,
                "con": False,
                "mc": [
                    {
                        "id": "1.A",
                        "img": True,
                        "rc": [{"id": 1, "hc": 0, "ltp": 2.0}],
                    }
                ],
            },
            received_time_ms=1001,
            ingested_time_ms=1002,
        )
    except (TypeError, ValueError):
        # Rejecting caller-authored provenance at construction/ingestion is safe.
        return

    identity = BetfairQuoteIdentity(
        BETFAIR_STREAM_SOURCE_ID,
        "1.A",
        1,
        Decimal("0"),
        BetfairQuoteSide.LAST_TRADED,
        None,
    )
    decision = runtime.evaluate(
        identity,
        as_of_ms=1005,
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10),
    )

    assert not decision.decision_eligible
