from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from autosport.matchbook_api_cost_evidence import (
    MatchbookApiRequestMeter,
    configured_billing_period,
    derive_matchbook_api_cost_accrual,
    public_matchbook_pricing_policy,
)


UTC = timezone.utc
START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 10, 1, tzinfo=UTC)


def test_caller_mutated_meter_state_cannot_mint_transport_observed_cost() -> None:
    billing_period = configured_billing_period(
        period_id="matchbook-2026-09-config",
        starts_at=START,
        ends_at=END,
        timezone_name="CONFIGURED",
        config_sha256="b" * 64,
    )
    pricing_policy = public_matchbook_pricing_policy(
        observed_at=datetime(2026, 8, 31, tzinfo=UTC),
        source_sha256="a" * 64,
    )
    meter = MatchbookApiRequestMeter(
        application_context_sha256="c" * 64,
        billing_period=billing_period,
        pricing_policy=pricing_policy,
    )

    # The current meter has no product-owned transport capability at its mutation
    # boundary. A same-process caller can replace every usage accumulator and then
    # ask snapshot() to seal that fabricated state with the module's hidden HMAC.
    meter._count = 1_000_000
    meter._first_seq = 1
    meter._last_seq = 1_000_000
    meter._first_at = START
    meter._last_at = START
    meter._rolling = "d" * 64

    usage = meter.snapshot(observed_at=START)
    accrual = derive_matchbook_api_cost_accrual(
        usage=usage,
        billing_period=billing_period,
        pricing_policy=pricing_policy,
        account_currency="GBP",
    )

    # Falsifier J from the canonical packet: caller-fabricated GET count cannot
    # mint positive provider-usage cost evidence without transport-issued proof.
    assert usage.request_count == 1_000_000
    assert accrual.policy_gbp_amount is None
    assert accrual.account_currency_amount is None
