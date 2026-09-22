from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betfair_cancel_saga import (
    BetfairCancelSagaError,
    BetfairCancelSagaStore,
    CancelAuthorityScope,
    CancelIntent,
    CancelOrderEvidence,
    CancelOrderType,
    CancelReconciliationEvidence,
    CancelRetryDisposition,
    CancelScope,
    CancelSagaState,
)


PREPARED = "2026-09-21T12:10:00+00:00"
SUBMITTED = "2026-09-21T12:10:01+00:00"
RECONCILED = "2026-09-21T12:10:03+00:00"
SHA_A = "a" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _source_order() -> CancelOrderEvidence:
    return CancelOrderEvidence(
        account_id="acct-1",
        market_id="1.234567",
        selection_id="12345",
        side="BACK",
        bet_id="bet-1",
        order_type=CancelOrderType.LIMIT,
        size_matched=Decimal("2"),
        size_remaining=Decimal("8"),
        size_cancelled=Decimal("0"),
        observed_at="2026-09-21T12:09:59+00:00",
        evidence_id=SHA_A,
    )


def _intent() -> CancelIntent:
    return CancelIntent(
        saga_id="cancel-saga-opaque-final",
        account_id="acct-1",
        environment="supervised-live",
        scope=CancelScope.SINGLE_ORDER,
        authority_scope=CancelAuthorityScope.SINGLE_ORDER,
        authority_ref="owner-goal:goal-1:revision:7",
        authority_sha256=SHA_A,
        prepared_at=PREPARED,
        market_id="1.234567",
        bet_id="bet-1",
        source_order=_source_order(),
    )


def test_opaque_final_digest_cannot_prove_disappeared_order_was_cancelled(tmp_path):
    """A digest cannot distinguish CANCELLED from matched/lapsed/void terminal truth.

    If the contract evolves to reject this evidence shape at construction or
    adoption time, that fail-closed behavior satisfies this falsifier. If it
    still accepts the shape, it must not mint terminal cancellation authority.
    """

    store = BetfairCancelSagaStore(tmp_path / "cancel-sagas.jsonl")
    intent = _intent()
    store.prepare(intent)
    store.mark_submitted(intent.saga_id, submitted_at=SUBMITTED)

    try:
        opaque_final = CancelReconciliationEvidence(
            evidence_id=SHA_D,
            observed_at=RECONCILED,
            source="betfair:listCurrentOrders+listClearedOrders",
            scope_complete=True,
            orders=(),
            absent_bet_ids=(intent.bet_id,),
            final_evidence_sha256=SHA_C,
        )
        snapshot = store.record_reconciliation(intent.saga_id, opaque_final)
    except (ValueError, BetfairCancelSagaError):
        return

    assert snapshot.state is not CancelSagaState.CANCEL_RECONCILED
    assert snapshot.exposure.provider_verified is False
    assert snapshot.retry_disposition is not CancelRetryDisposition.TERMINAL_NO_RETRY
