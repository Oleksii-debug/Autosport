from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from autosport.bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.provider_balance_projection import (
    GlobalProviderBalanceProjection,
    ProviderBalanceProjectionComponent,
    ProviderLiquidityError,
    project_provider_balances,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def _snapshot(
    *,
    venue: str = "betfair",
    account: str = "acct-1",
    adapter: str = "adapter-1",
    currency: str = "USD",
    available: str = "100.00",
    exposure: str | None = "-25.00",
    balance_at: str = "2026-09-21T12:00:00+00:00",
    snapshot_at: str = "2026-09-21T12:00:01+00:00",
    observation_id: str = "balance-1",
    source_sha: str = SHA_A,
    profile_at: str = "2026-09-21T11:59:59+00:00",
) -> BookmakerAccountSnapshot:
    profile = BookmakerCapabilityProfile(
        venue_id=venue,
        account_id=account,
        adapter_id=adapter,
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                capability=BookmakerCapability.BALANCE_READ,
                state=BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=profile_at,
        source_ref="caller-supplied-account-contract",
        source_payload_sha256=source_sha,
    )
    balance = BookmakerBalanceObservation(
        venue_id=venue,
        account_id=account,
        adapter_id=adapter,
        observation_id=observation_id,
        currency=currency,
        available_balance=Decimal(available),
        observed_at=balance_at,
        source_payload_sha256=source_sha,
        exposure=None if exposure is None else Decimal(exposure),
        retained_commission=Decimal("1.25"),
        exposure_limit=Decimal("-500"),
    )
    return BookmakerAccountSnapshot(
        profile=profile,
        observed_capabilities=frozenset({BookmakerCapability.BALANCE_READ}),
        observed_at=snapshot_at,
        balance=balance,
    )


def test_same_currency_projection_preserves_supplied_balance_without_double_subtracting_exposure() -> None:
    first = _snapshot(available="100", exposure="-25")
    second = _snapshot(
        venue="other",
        account="acct-2",
        adapter="adapter-2",
        available="50",
        exposure="-10",
        observation_id="balance-2",
        source_sha=SHA_B,
    )

    projection = project_provider_balances(
        (first, second),
        decision_at="2026-09-21T12:00:02+00:00",
        max_age=timedelta(seconds=5),
    )

    assert projection.supplied_balance_in("USD") == Decimal("150")
    assert projection.single_currency_supplied_balance() == Decimal("150")
    assert [item.supplied_exposure for item in projection.components] == [
        Decimal("-25"),
        Decimal("-10"),
    ]
    assert not hasattr(projection, "total")
    assert not hasattr(projection, "exposure_adjusted_total")
    assert not hasattr(projection.components[0], "available_to_execute")


def test_caller_minted_shape_valid_snapshot_stays_non_authoritative() -> None:
    caller_minted = _snapshot(
        available="999999",
        source_sha="f" * 64,
        observation_id="caller-minted-balance",
    )

    projection = project_provider_balances(
        (caller_minted,),
        decision_at="2026-09-21T12:00:02+00:00",
        max_age=timedelta(seconds=5),
    )

    assert projection.supplied_balance_in("USD") == Decimal("999999")
    assert projection.source_authority_proven is False
    assert projection.allocation_authority_proven is False
    assert projection.atomicity_proven is False
    assert projection.components[0].source_authority_proven is False
    canonical = projection.to_canonical_dict()
    assert canonical["source_authority_proven"] is False
    assert canonical["allocation_authority_proven"] is False
    assert not hasattr(projection, "executable_balance")
    assert not hasattr(projection, "authorize_allocation")


def test_mixed_currency_stays_typed_and_has_no_unqualified_scalar_total() -> None:
    usd = _snapshot(currency="USD")
    eur = _snapshot(
        venue="other",
        account="acct-2",
        adapter="adapter-2",
        currency="EUR",
        available="80",
        observation_id="balance-2",
        source_sha=SHA_B,
    )

    projection = project_provider_balances(
        (usd, eur),
        decision_at="2026-09-21T12:00:02+00:00",
        max_age=timedelta(seconds=5),
    )

    assert projection.currencies == ("EUR", "USD")
    assert projection.supplied_balance_in("USD") == Decimal("100.00")
    assert projection.supplied_balance_in("EUR") == Decimal("80")
    with pytest.raises(ProviderLiquidityError, match="mixed-currency"):
        projection.single_currency_supplied_balance()


def test_missing_currency_is_unknown_not_zero() -> None:
    projection = project_provider_balances(
        (_snapshot(),),
        decision_at="2026-09-21T12:00:02+00:00",
        max_age=timedelta(seconds=5),
    )

    with pytest.raises(ProviderLiquidityError, match="no fresh supplied balance"):
        projection.supplied_balance_in("EUR")


def test_empty_observations_are_unknown_not_zero() -> None:
    with pytest.raises(ProviderLiquidityError, match="absence is unknown"):
        project_provider_balances(
            (),
            decision_at="2026-09-21T12:00:02+00:00",
            max_age=timedelta(seconds=5),
        )


def test_stale_balance_fails_closed() -> None:
    with pytest.raises(ProviderLiquidityError, match="stale"):
        project_provider_balances(
            (_snapshot(balance_at="2026-09-21T11:59:00+00:00"),),
            decision_at="2026-09-21T12:00:02+00:00",
            max_age=timedelta(seconds=30),
        )


def test_one_stale_component_rejects_entire_projection_instead_of_omitting_it() -> None:
    fresh = _snapshot()
    stale = _snapshot(
        venue="other",
        account="acct-2",
        adapter="adapter-2",
        available="50",
        balance_at="2026-09-21T11:00:00+00:00",
        snapshot_at="2026-09-21T11:00:01+00:00",
        profile_at="2026-09-21T10:59:59+00:00",
        observation_id="balance-2",
        source_sha=SHA_B,
    )

    with pytest.raises(ProviderLiquidityError, match="stale"):
        project_provider_balances(
            (fresh, stale),
            decision_at="2026-09-21T12:00:02+00:00",
            max_age=timedelta(minutes=5),
        )


def test_future_snapshot_fails_closed() -> None:
    with pytest.raises(ProviderLiquidityError, match="snapshot is from the future"):
        project_provider_balances(
            (_snapshot(snapshot_at="2026-09-21T12:00:03+00:00"),),
            decision_at="2026-09-21T12:00:02+00:00",
            max_age=timedelta(seconds=30),
        )


def test_duplicate_provider_account_scope_fails_closed_even_via_second_adapter() -> None:
    first = _snapshot()
    second = _snapshot(
        adapter="adapter-2",
        observation_id="balance-2",
        source_sha=SHA_B,
    )

    with pytest.raises(ProviderLiquidityError, match="double-count"):
        project_provider_balances(
            (first, second),
            decision_at="2026-09-21T12:00:02+00:00",
            max_age=timedelta(seconds=5),
        )


def test_projection_identity_is_deterministic_under_input_order() -> None:
    first = _snapshot()
    second = _snapshot(
        venue="other",
        account="acct-2",
        adapter="adapter-2",
        available="50",
        observation_id="balance-2",
        source_sha=SHA_B,
    )
    kwargs = {
        "decision_at": "2026-09-21T12:00:02+00:00",
        "max_age": timedelta(seconds=5),
    }

    left = project_provider_balances((first, second), **kwargs)
    right = project_provider_balances((second, first), **kwargs)

    assert left.projection_id == right.projection_id
    assert left.to_canonical_dict() == right.to_canonical_dict()


def test_equal_provider_timestamps_do_not_mint_cross_provider_atomicity() -> None:
    first = _snapshot(balance_at="2026-09-21T12:00:00+00:00")
    second = _snapshot(
        venue="other",
        account="acct-2",
        adapter="adapter-2",
        balance_at="2026-09-21T14:00:00+02:00",
        snapshot_at="2026-09-21T14:00:01+02:00",
        observation_id="balance-2",
        source_sha=SHA_B,
    )

    projection = project_provider_balances(
        (first, second),
        decision_at="2026-09-21T12:00:02+00:00",
        max_age=timedelta(seconds=5),
    )

    assert projection.observation_span_seconds == Decimal("0")
    assert projection.atomicity_proven is False
    assert projection.to_canonical_dict()["atomicity_proven"] is False


def test_negative_freshness_window_is_rejected() -> None:
    with pytest.raises(ProviderLiquidityError, match="non-negative"):
        project_provider_balances(
            (_snapshot(),),
            decision_at="2026-09-21T12:00:02+00:00",
            max_age=timedelta(seconds=-1),
        )


def test_direct_positive_authority_construction_is_not_public() -> None:
    with pytest.raises(TypeError):
        ProviderBalanceProjectionComponent()
    with pytest.raises(TypeError):
        GlobalProviderBalanceProjection()


def test_component_id_binds_supplied_observation_fields() -> None:
    first = project_provider_balances(
        (_snapshot(exposure="-25"),),
        decision_at="2026-09-21T12:00:02+00:00",
        max_age=timedelta(seconds=5),
    ).components[0]
    second = project_provider_balances(
        (_snapshot(exposure="-26"),),
        decision_at="2026-09-21T12:00:02+00:00",
        max_age=timedelta(seconds=5),
    ).components[0]

    assert first.component_id != second.component_id
