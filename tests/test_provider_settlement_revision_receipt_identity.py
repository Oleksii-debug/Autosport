from decimal import Decimal

import pytest

from autosport.bookmaker_capability import (
    BookmakerPositionObservation,
    BookmakerPositionState,
)
from autosport.provider_settlement_revision import (
    ProviderSettlementRevision,
    ProviderSettlementRevisionChain,
    ProviderSettlementRevisionError,
)


T1 = "2026-09-25T00:00:00+00:00"
T2 = "2026-09-25T00:01:00+00:00"


def _settled(*, observation_id: str, observed_at: str, gross_return: str, receipt: str):
    return BookmakerPositionObservation(
        venue_id="betfair-exchange",
        account_id="acct-a",
        adapter_id="betfair-read",
        observation_id=observation_id,
        external_position_id="bet-1",
        state=BookmakerPositionState.SETTLED,
        currency="EUR",
        observed_at=observed_at,
        source_payload_sha256=("a" if observation_id == "settlement-1" else "b") * 64,
        provider_amount=Decimal("10"),
        provider_amount_semantics="backer_stake",
        provider_side="BACK",
        decimal_odds=Decimal("2.00"),
        gross_return=Decimal(gross_return),
        external_receipt_id=receipt,
    )


def test_resettlement_preserves_execution_receipt_identity() -> None:
    first = ProviderSettlementRevision(
        settlement=_settled(
            observation_id="settlement-1",
            observed_at=T1,
            gross_return="20",
            receipt="execution-receipt-1",
        ),
        available_at=T1,
        source_ref="cleared/settlement-1",
    )
    corrected = ProviderSettlementRevision(
        settlement=_settled(
            observation_id="settlement-2",
            observed_at=T2,
            gross_return="12.50",
            receipt="execution-receipt-1",
        ),
        available_at=T2,
        source_ref="cleared/settlement-2",
        supersedes_revision_id=first.revision_id,
    )

    chain = ProviderSettlementRevisionChain((first, corrected))

    assert chain.current_restated == corrected
    assert chain.current_restated.settlement.external_receipt_id == "execution-receipt-1"


def test_resettlement_cannot_rewrite_execution_receipt_identity() -> None:
    first = ProviderSettlementRevision(
        settlement=_settled(
            observation_id="settlement-1",
            observed_at=T1,
            gross_return="20",
            receipt="execution-receipt-1",
        ),
        available_at=T1,
        source_ref="cleared/settlement-1",
    )
    corrected = ProviderSettlementRevision(
        settlement=_settled(
            observation_id="settlement-2",
            observed_at=T2,
            gross_return="12.50",
            receipt="different-execution-receipt",
        ),
        available_at=T2,
        source_ref="cleared/settlement-2",
        supersedes_revision_id=first.revision_id,
    )

    with pytest.raises(
        ProviderSettlementRevisionError,
        match="executed position terms",
    ):
        ProviderSettlementRevisionChain((first, corrected))
