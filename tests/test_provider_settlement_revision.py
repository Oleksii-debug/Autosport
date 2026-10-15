from decimal import Decimal, localcontext

import pytest

from autosport.bookmaker_capability import (
    BookmakerPositionObservation,
    BookmakerPositionState,
)
from autosport.provider_settlement_revision import (
    ProviderSettlementRevision,
    ProviderSettlementRevisionChain,
    ProviderSettlementRevisionError,
    build_provider_settlement_revision_chain,
)


_T1 = "2026-09-21T08:00:00+00:00"
_T2 = "2026-09-21T09:00:00+00:00"
_T3 = "2026-09-21T10:00:00+00:00"
_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64


def _settled(
    *,
    observation_id: str,
    observed_at: str,
    gross_return: str,
    source_hash: str = _HASH_A,
    venue_id: str = "betfair-exchange",
    account_id: str = "acct-a",
    adapter_id: str = "betfair-read",
    position_id: str = "bet-1",
    currency: str = "EUR",
    state: BookmakerPositionState = BookmakerPositionState.SETTLED,
    provider_amount: str = "10",
    provider_amount_semantics: str = "backer_stake",
    provider_side: str | None = "BACK",
    decimal_odds: str | None = "2.00",
    external_receipt_id: str | None = None,
) -> BookmakerPositionObservation:
    return BookmakerPositionObservation(
        venue_id=venue_id,
        account_id=account_id,
        adapter_id=adapter_id,
        observation_id=observation_id,
        external_position_id=position_id,
        state=state,
        currency=currency,
        observed_at=observed_at,
        source_payload_sha256=source_hash,
        provider_amount=Decimal(provider_amount),
        provider_amount_semantics=provider_amount_semantics,
        provider_side=provider_side,
        decimal_odds=Decimal(decimal_odds) if decimal_odds is not None else None,
        gross_return=Decimal(gross_return),
        external_receipt_id=(
            external_receipt_id
            if external_receipt_id is not None
            else f"receipt-{observation_id}"
        ),
    )


def _revision(
    observation: BookmakerPositionObservation,
    *,
    available_at: str,
    supersedes: str | None = None,
    source_ref: str | None = None,
) -> ProviderSettlementRevision:
    return ProviderSettlementRevision(
        settlement=observation,
        available_at=available_at,
        source_ref=source_ref or f"cleared/{observation.observation_id}",
        supersedes_revision_id=supersedes,
    )


def test_initial_settlement_is_both_historical_and_current_view() -> None:
    first = _revision(
        _settled(observation_id="settlement-1", observed_at=_T1, gross_return="20"),
        available_at=_T1,
    )
    chain = build_provider_settlement_revision_chain((first,))

    assert chain.current_restated == first
    assert chain.as_known_at(_T1) == first
    assert chain.as_known_at("2026-09-21T07:59:59+00:00") is None


def test_later_resettlement_changes_current_without_leaking_into_old_cutoff() -> None:
    first = _revision(
        _settled(observation_id="settlement-1", observed_at=_T1, gross_return="20"),
        available_at=_T1,
    )
    corrected = _revision(
        _settled(
            observation_id="settlement-2",
            observed_at=_T2,
            gross_return="12.50",
            source_hash=_HASH_B,
        ),
        available_at=_T2,
        supersedes=first.revision_id,
    )
    chain = build_provider_settlement_revision_chain((first, corrected))

    assert chain.as_known_at("2026-09-21T08:30:00+00:00") == first
    assert chain.current_restated == corrected
    assert chain.current_restated.settlement.gross_return == Decimal("12.50")


def test_loss_can_be_corrected_to_positive_provider_return_without_reopening_position() -> None:
    first = _revision(
        _settled(observation_id="settlement-1", observed_at=_T1, gross_return="0"),
        available_at=_T1,
    )
    corrected = _revision(
        _settled(
            observation_id="settlement-2",
            observed_at=_T2,
            gross_return="18",
            source_hash=_HASH_B,
        ),
        available_at=_T2,
        supersedes=first.revision_id,
    )
    chain = ProviderSettlementRevisionChain((first, corrected))

    assert all(
        revision.settlement.state is BookmakerPositionState.SETTLED
        for revision in chain.revisions
    )
    assert chain.current_restated.settlement.gross_return == Decimal("18")


def test_open_position_cannot_enter_settlement_revision_chain() -> None:
    with pytest.raises(
        ProviderSettlementRevisionError,
        match="requires terminal SETTLED",
    ):
        _revision(
            _settled(
                observation_id="open-1",
                observed_at=_T1,
                gross_return="0",
                state=BookmakerPositionState.OPEN,
            ),
            available_at=_T1,
        )


def test_exact_duplicate_revision_is_idempotently_collapsed() -> None:
    first = _revision(
        _settled(observation_id="settlement-1", observed_at=_T1, gross_return="20"),
        available_at=_T1,
    )
    chain = ProviderSettlementRevisionChain((first, first))

    assert chain.revisions == (first,)


def test_conflicting_reuse_of_provider_observation_id_fails_closed() -> None:
    first = _revision(
        _settled(observation_id="settlement-1", observed_at=_T1, gross_return="20"),
        available_at=_T1,
    )
    conflicting = _revision(
        _settled(
            observation_id="settlement-1",
            observed_at=_T2,
            gross_return="10",
            source_hash=_HASH_B,
        ),
        available_at=_T2,
        supersedes=first.revision_id,
    )

    with pytest.raises(
        ProviderSettlementRevisionError,
        match="observation_id was reused with conflicting content",
    ):
        ProviderSettlementRevisionChain((first, conflicting))


def test_same_provider_observation_cannot_inflate_revision_count() -> None:
    first = _revision(
        _settled(observation_id="settlement-1", observed_at=_T1, gross_return="20"),
        available_at=_T1,
    )
    repeated_as_new_revision = _revision(
        first.settlement,
        available_at=_T2,
        supersedes=first.revision_id,
        source_ref="cleared/replayed-settlement-1",
    )

    with pytest.raises(
        ProviderSettlementRevisionError,
        match="cannot create a distinct settlement revision",
    ):
        ProviderSettlementRevisionChain((first, repeated_as_new_revision))


def test_missing_or_forked_predecessor_fails_closed() -> None:
    first = _revision(
        _settled(observation_id="settlement-1", observed_at=_T1, gross_return="20"),
        available_at=_T1,
    )
    orphan = _revision(
        _settled(
            observation_id="settlement-2",
            observed_at=_T2,
            gross_return="10",
            source_hash=_HASH_B,
        ),
        available_at=_T2,
        supersedes="f" * 64,
    )

    with pytest.raises(
        ProviderSettlementRevisionError,
        match="predecessor is missing, forked, or out of order",
    ):
        ProviderSettlementRevisionChain((first, orphan))


def test_first_revision_cannot_claim_unseen_predecessor() -> None:
    first = _revision(
        _settled(observation_id="settlement-1", observed_at=_T1, gross_return="20"),
        available_at=_T1,
        supersedes="f" * 64,
    )

    with pytest.raises(
        ProviderSettlementRevisionError,
        match="cannot supersede an unseen predecessor",
    ):
        ProviderSettlementRevisionChain((first,))


def test_correction_cannot_be_available_before_observation() -> None:
    with pytest.raises(
        ProviderSettlementRevisionError,
        match="cannot be available before it was observed",
    ):
        _revision(
            _settled(observation_id="settlement-1", observed_at=_T2, gross_return="20"),
            available_at=_T1,
        )


def test_distinct_revisions_require_unambiguous_increasing_availability() -> None:
    first = _revision(
        _settled(observation_id="settlement-1", observed_at=_T1, gross_return="20"),
        available_at=_T2,
    )
    corrected = _revision(
        _settled(
            observation_id="settlement-2",
            observed_at=_T2,
            gross_return="10",
            source_hash=_HASH_B,
        ),
        available_at=_T2,
        supersedes=first.revision_id,
    )

    with pytest.raises(
        ProviderSettlementRevisionError,
        match="strictly increasing causal availability",
    ):
        ProviderSettlementRevisionChain((first, corrected))


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("account", {"account_id": "acct-b"}),
        ("adapter", {"adapter_id": "other-adapter"}),
        ("position", {"position_id": "bet-2"}),
        ("currency", {"currency": "GBP"}),
    ],
)
def test_correction_cannot_rebind_core_identity(field: str, kwargs: dict[str, str]) -> None:
    first = _revision(
        _settled(observation_id="settlement-1", observed_at=_T1, gross_return="20"),
        available_at=_T1,
    )
    corrected = _revision(
        _settled(
            observation_id="settlement-2",
            observed_at=_T2,
            gross_return="10",
            source_hash=_HASH_B,
            **kwargs,
        ),
        available_at=_T2,
        supersedes=first.revision_id,
    )

    with pytest.raises(
        ProviderSettlementRevisionError,
        match="cannot rewrite provider/account/adapter/position/currency identity",
    ):
        ProviderSettlementRevisionChain((first, corrected))


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("provider amount", {"provider_amount": "11"}),
        (
            "provider amount semantics",
            {"provider_amount_semantics": "lay_liability"},
        ),
        ("provider side", {"provider_side": "LAY"}),
        ("decimal odds", {"decimal_odds": "2.10"}),
    ],
)
def test_correction_cannot_rewrite_executed_position_terms(
    field: str,
    kwargs: dict[str, str],
) -> None:
    first = _revision(
        _settled(observation_id="settlement-1", observed_at=_T1, gross_return="20"),
        available_at=_T1,
    )
    corrected = _revision(
        _settled(
            observation_id="settlement-2",
            observed_at=_T2,
            gross_return="10",
            source_hash=_HASH_B,
            **kwargs,
        ),
        available_at=_T2,
        supersedes=first.revision_id,
    )

    with pytest.raises(
        ProviderSettlementRevisionError,
        match="executed position terms",
    ):
        ProviderSettlementRevisionChain((first, corrected))


def test_correction_may_use_new_observation_receipt_when_execution_terms_match() -> None:
    first = _revision(
        _settled(
            observation_id="settlement-1",
            observed_at=_T1,
            gross_return="20",
            external_receipt_id="settlement-receipt-1",
        ),
        available_at=_T1,
    )
    corrected = _revision(
        _settled(
            observation_id="settlement-2",
            observed_at=_T2,
            gross_return="10",
            source_hash=_HASH_B,
            external_receipt_id="settlement-receipt-2",
        ),
        available_at=_T2,
        supersedes=first.revision_id,
    )

    chain = ProviderSettlementRevisionChain((first, corrected))

    assert chain.current_restated == corrected
    assert first.settlement.external_receipt_id == "settlement-receipt-1"
    assert corrected.settlement.external_receipt_id == "settlement-receipt-2"


def test_correction_observation_time_cannot_move_backward() -> None:
    first = _revision(
        _settled(observation_id="settlement-1", observed_at=_T2, gross_return="20"),
        available_at=_T2,
    )
    corrected = _revision(
        _settled(
            observation_id="settlement-2",
            observed_at=_T1,
            gross_return="10",
            source_hash=_HASH_B,
        ),
        available_at=_T3,
        supersedes=first.revision_id,
    )

    with pytest.raises(
        ProviderSettlementRevisionError,
        match="observation time cannot move backward",
    ):
        ProviderSettlementRevisionChain((first, corrected))


def test_revision_identity_is_decimal_context_independent() -> None:
    observation = _settled(
        observation_id="settlement-1",
        observed_at=_T1,
        gross_return="12345678901234567890.1234500",
    )
    with localcontext() as context:
        context.prec = 6
        low_precision = _revision(observation, available_at=_T1).revision_id
    with localcontext() as context:
        context.prec = 50
        high_precision = _revision(observation, available_at=_T1).revision_id

    assert low_precision == high_precision


def test_chain_identity_changes_when_correction_is_appended() -> None:
    first = _revision(
        _settled(observation_id="settlement-1", observed_at=_T1, gross_return="20"),
        available_at=_T1,
    )
    before = ProviderSettlementRevisionChain((first,))
    corrected = _revision(
        _settled(
            observation_id="settlement-2",
            observed_at=_T2,
            gross_return="10",
            source_hash=_HASH_C,
        ),
        available_at=_T2,
        supersedes=first.revision_id,
    )
    after = ProviderSettlementRevisionChain((first, corrected))

    assert before.chain_id != after.chain_id


def test_caller_subclass_cannot_mint_positive_structural_settlement() -> None:
    class ForgedPosition(BookmakerPositionObservation):
        pass

    forged = ForgedPosition(
        venue_id="betfair-exchange",
        account_id="acct-a",
        adapter_id="betfair-read",
        observation_id="settlement-1",
        external_position_id="bet-1",
        state=BookmakerPositionState.SETTLED,
        currency="EUR",
        observed_at=_T1,
        source_payload_sha256=_HASH_A,
        provider_amount=Decimal("10"),
        provider_amount_semantics="backer_stake",
        provider_side="BACK",
        decimal_odds=Decimal("2"),
        gross_return=Decimal("20"),
        external_receipt_id="receipt-1",
    )

    with pytest.raises(
        ProviderSettlementRevisionError,
        match="exact BookmakerPositionObservation",
    ):
        _revision(forged, available_at=_T1)
