from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betfair_cancel_saga import (
    BetfairCancelSagaStore,
    CancelAuthorityScope,
    CancelIntent,
    CancelOrderEvidence,
    CancelOrderReadback,
    CancelOrderType,
    CancelReconciliationEvidence,
    CancelScope,
)

PREPARED = "2026-09-22T20:00:00+00:00"
SUBMITTED = "2026-09-22T20:00:01+00:00"
RECONCILED = "2026-09-22T20:00:02+00:00"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _intent() -> CancelIntent:
    source = CancelOrderEvidence(
        account_id="acct-1",
        market_id="1.234567",
        selection_id="12345",
        side="BACK",
        bet_id="bet-1",
        order_type=CancelOrderType.LIMIT,
        size_matched=Decimal("2"),
        size_remaining=Decimal("8"),
        size_cancelled=Decimal("0"),
        observed_at="2026-09-22T19:59:59+00:00",
        evidence_id=SHA_A,
    )
    return CancelIntent(
        saga_id="cancel-origin-falsifier",
        account_id="acct-1",
        environment="supervised-live",
        scope=CancelScope.SINGLE_ORDER,
        authority_scope=CancelAuthorityScope.SINGLE_ORDER,
        authority_ref="caller-shaped-authority",
        authority_sha256=SHA_A,
        prepared_at=PREPARED,
        market_id="1.234567",
        bet_id="bet-1",
        source_order=source,
    )


def _submitted(tmp_path):
    store = BetfairCancelSagaStore(tmp_path / "cancel-sagas.jsonl")
    intent = _intent()
    store.prepare(intent)
    store.mark_submitted(intent.saga_id, submitted_at=SUBMITTED)
    return store, intent


def test_caller_minted_final_absence_cannot_become_verified_cancellation(tmp_path) -> None:
    store, intent = _submitted(tmp_path)

    # No canonical Betfair adapter/client issued this object. The evidence id,
    # completeness bit and "final" SHA are arbitrary caller strings that merely
    # satisfy shape validation.
    forged = CancelReconciliationEvidence(
        evidence_id=SHA_B,
        observed_at=RECONCILED,
        source="caller:forged-final-absence",
        scope_complete=True,
        orders=(),
        absent_bet_ids=(intent.bet_id,),
        final_evidence_sha256=SHA_C,
    )

    with pytest.raises(Exception):
        store.record_reconciliation(intent.saga_id, forged)


def test_caller_minted_order_components_cannot_become_verified_cancellation(tmp_path) -> None:
    store, intent = _submitted(tmp_path)

    # These economics are likewise not re-derived from canonical readback. A
    # caller can currently state the exact delta needed to satisfy cancellation
    # and ask the saga to promote it.
    forged_order = CancelOrderReadback(
        account_id="acct-1",
        market_id="1.234567",
        selection_id="12345",
        side="BACK",
        bet_id="bet-1",
        order_type=CancelOrderType.LIMIT,
        size_matched=Decimal("2"),
        size_remaining=Decimal("0"),
        size_cancelled=Decimal("8"),
        size_lapsed=Decimal("0"),
        size_voided=Decimal("0"),
    )
    forged = CancelReconciliationEvidence(
        evidence_id=SHA_B,
        observed_at=RECONCILED,
        source="caller:forged-order-readback",
        scope_complete=True,
        orders=(forged_order,),
    )

    with pytest.raises(Exception):
        store.record_reconciliation(intent.saga_id, forged)
