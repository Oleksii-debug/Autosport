from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.betfair_cancel_saga import (
    BetfairCancelSagaIdentityConflict,
    BetfairCancelSagaIntegrityError,
    BetfairCancelSagaStateError,
    BetfairCancelSagaStore,
    CancelAuthorityScope,
    CancelIntent,
    CancelOrderEvidence,
    CancelOrderReadback,
    CancelOrderType,
    CancelProviderEvidence,
    CancelProviderStatus,
    CancelReconciliationEvidence,
    CancelRetryDisposition,
    CancelScope,
    CancelSagaState,
)

PREPARED = "2026-09-21T12:10:00+00:00"
SUBMITTED = "2026-09-21T12:10:01+00:00"
OBSERVED = "2026-09-21T12:10:02+00:00"
RECONCILED = "2026-09-21T12:10:03+00:00"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def source_order(*, remaining="8", matched="2", order_type=CancelOrderType.LIMIT):
    return CancelOrderEvidence(
        account_id="acct-1",
        market_id="1.234567",
        selection_id="12345",
        side="BACK",
        bet_id="bet-1",
        order_type=order_type,
        size_matched=Decimal(matched),
        size_remaining=Decimal(remaining),
        size_cancelled=Decimal("0"),
        observed_at="2026-09-21T12:09:59+00:00",
        evidence_id=SHA_A,
    )


def single_intent(*, cancel_size=None, source=None):
    return CancelIntent(
        saga_id="cancel-saga-1",
        account_id="acct-1",
        environment="supervised-live",
        scope=CancelScope.SINGLE_ORDER,
        authority_scope=CancelAuthorityScope.SINGLE_ORDER,
        authority_ref="owner-goal:goal-1:revision:7",
        authority_sha256=SHA_A,
        prepared_at=PREPARED,
        market_id="1.234567",
        bet_id="bet-1",
        cancel_size=cancel_size,
        source_order=source or source_order(),
    )


def market_intent():
    return CancelIntent(
        saga_id="cancel-market-1",
        account_id="acct-1",
        environment="supervised-live",
        scope=CancelScope.MARKET_UNMATCHED,
        authority_scope=CancelAuthorityScope.MARKET_UNMATCHED,
        authority_ref="safety:market:1.234567",
        authority_sha256=SHA_A,
        prepared_at=PREPARED,
        market_id="1.234567",
    )


def account_intent():
    return CancelIntent(
        saga_id="cancel-account-1",
        account_id="acct-1",
        environment="supervised-live",
        scope=CancelScope.ACCOUNT_ALL_LIMIT,
        authority_scope=CancelAuthorityScope.ACCOUNT_EMERGENCY,
        authority_ref="emergency:acct-1",
        authority_sha256=SHA_A,
        prepared_at=PREPARED,
    )


def provider(status=CancelProviderStatus.SUCCESS, *, error_code=None):
    return CancelProviderEvidence(
        evidence_id=SHA_B,
        response_sha256=SHA_C,
        observed_at=OBSERVED,
        source="betfair:cancelOrders:response",
        status=status,
        error_code=error_code,
    )


def readback(*, remaining="0", matched="2", cancelled="8", account="acct-1",
             market="1.234567", bet="bet-1", selection="12345",
             side="BACK", order_type=CancelOrderType.LIMIT):
    return CancelOrderReadback(
        account_id=account,
        market_id=market,
        selection_id=selection,
        side=side,
        bet_id=bet,
        order_type=order_type,
        size_matched=Decimal(matched),
        size_remaining=Decimal(remaining),
        size_cancelled=Decimal(cancelled),
    )


def reconciliation(*, orders=(), absent=(), complete=True, final_sha=None,
                   evidence_id=SHA_D, observed_at=RECONCILED):
    return CancelReconciliationEvidence(
        evidence_id=evidence_id,
        observed_at=observed_at,
        source="betfair:listCurrentOrders+listClearedOrders",
        scope_complete=complete,
        orders=tuple(orders),
        absent_bet_ids=tuple(absent),
        final_evidence_sha256=final_sha,
    )


def submitted_store(tmp_path, intent=None):
    store = BetfairCancelSagaStore(tmp_path / "cancel-sagas.jsonl")
    value = intent or single_intent()
    store.prepare(value)
    store.mark_submitted(value.saga_id, submitted_at=SUBMITTED)
    return store, value


def test_single_order_request_is_narrow_and_never_uses_omitted_bet_id():
    value = single_intent(cancel_size=Decimal("3"))
    request = value.provider_request()
    assert request["method"] == "SportsAPING/v1.0/cancelOrders"
    assert request["params"]["marketId"] == "1.234567"
    assert request["params"]["instructions"] == [
        {"betId": "bet-1", "sizeReduction": "3"}
    ]
    assert len(request["params"]["customerRef"]) == 32
    assert value.serialization_key.endswith(":bet:bet-1")


def test_single_order_full_cancel_still_names_exact_bet_id():
    request = single_intent().provider_request()
    assert request["params"]["instructions"] == [{"betId": "bet-1"}]


def test_market_scope_deliberately_omits_instructions_but_requires_market_authority():
    value = market_intent()
    assert value.provider_request()["params"].keys() == {"marketId", "customerRef"}
    with pytest.raises(ValueError, match="authority scope"):
        replace(value, authority_scope=CancelAuthorityScope.SINGLE_ORDER)


def test_account_scope_deliberately_omits_market_and_requires_emergency_authority():
    value = account_intent()
    assert value.provider_request()["params"].keys() == {"customerRef"}
    assert value.serialization_key.endswith(":ACCOUNT_ALL_LIMIT")
    with pytest.raises(ValueError, match="authority scope"):
        replace(value, authority_scope=CancelAuthorityScope.MARKET_UNMATCHED)


def test_accidental_blast_radius_widening_fields_fail_closed():
    with pytest.raises(ValueError):
        replace(single_intent(), bet_id=None)
    with pytest.raises(ValueError):
        replace(market_intent(), bet_id="bet-1")
    with pytest.raises(ValueError):
        replace(account_intent(), market_id="1.234567")


def test_partial_cancel_cannot_exceed_authoritative_remaining_size():
    with pytest.raises(ValueError, match="exceeds"):
        single_intent(cancel_size=Decimal("8.01"))
    value = single_intent(cancel_size=Decimal("8"))
    assert value.cancel_size == Decimal("8")


def test_sp_orders_cannot_enter_ordinary_cancel_saga():
    with pytest.raises(ValueError, match="must be LIMIT"):
        source_order(order_type=CancelOrderType.LIMIT_ON_CLOSE)
    with pytest.raises(ValueError, match="must be LIMIT"):
        source_order(order_type=CancelOrderType.MARKET_ON_CLOSE)


def test_prepare_is_restart_stable_and_grants_no_execution_authority(tmp_path):
    store = BetfairCancelSagaStore(tmp_path / "cancel.jsonl")
    first = store.prepare(single_intent())
    restarted = BetfairCancelSagaStore(store.path).snapshot("cancel-saga-1")
    assert first.state is CancelSagaState.PREPARED
    assert restarted.state is CancelSagaState.PREPARED
    assert restarted.retry_disposition is CancelRetryDisposition.EXPLICIT_EXECUTION_POLICY_REQUIRED
    assert restarted.exposure.provider_verified is False
    assert restarted.exposure.execution_admission_eligible is False
    assert restarted.exposure.real_money_authorized is False
    assert restarted.exposure.portfolio_reset_proven is False


def test_changed_intent_cannot_reuse_saga_id(tmp_path):
    store = BetfairCancelSagaStore(tmp_path / "cancel.jsonl")
    store.prepare(single_intent())
    with pytest.raises(BetfairCancelSagaIdentityConflict, match="different immutable"):
        store.prepare(single_intent(cancel_size=Decimal("1")))


def test_submitted_restart_requires_readback_and_preserves_matched_exposure(tmp_path):
    store, _ = submitted_store(tmp_path)
    snap = BetfairCancelSagaStore(store.path).snapshot("cancel-saga-1")
    assert snap.state is CancelSagaState.SUBMITTED_UNKNOWN
    assert snap.retry_disposition is CancelRetryDisposition.READBACK_REQUIRED
    assert snap.exposure.matched_bet_ids == ("bet-1",)
    assert snap.exposure.executable_bet_ids == ("bet-1",)
    assert snap.exposure.unresolved_bet_ids == ("bet-1",)


def test_timeout_is_not_retry_permission_and_survives_restart(tmp_path):
    store, _ = submitted_store(tmp_path)
    snap = store.mark_unknown(
        "cancel-saga-1",
        reason="cancelOrders timeout",
        observed_at=OBSERVED,
    )
    assert snap.state is CancelSagaState.SUBMITTED_UNKNOWN
    assert snap.retry_disposition is CancelRetryDisposition.READBACK_REQUIRED
    restarted = BetfairCancelSagaStore(store.path).snapshot("cancel-saga-1")
    assert restarted.retry_disposition is CancelRetryDisposition.READBACK_REQUIRED


def test_provider_success_requires_readback_and_does_not_erase_exposure(tmp_path):
    store, _ = submitted_store(tmp_path)
    snap = store.record_provider_result("cancel-saga-1", provider())
    assert snap.state is CancelSagaState.PROVIDER_RESULT_UNVERIFIED
    assert snap.retry_disposition is CancelRetryDisposition.READBACK_REQUIRED
    assert snap.exposure.matched_bet_ids == ("bet-1",)
    assert snap.exposure.executable_bet_ids == ("bet-1",)


def test_partial_cancel_reconciliation_retains_matched_liability(tmp_path):
    intent = single_intent(cancel_size=Decimal("3"))
    store, _ = submitted_store(tmp_path, intent)
    store.record_provider_result("cancel-saga-1", provider())
    snap = store.record_reconciliation(
        "cancel-saga-1",
        reconciliation(orders=(readback(remaining="5", matched="2", cancelled="3"),)),
    )
    assert snap.state is CancelSagaState.CANCEL_RECONCILED
    assert snap.exposure.matched_bet_ids == ("bet-1",)
    assert snap.exposure.executable_bet_ids == ("bet-1",)
    assert snap.exposure.cancelled_remainder_bet_ids == ("bet-1",)
    assert snap.exposure.provider_verified is True


def test_full_cancel_after_partial_match_is_not_no_exposure(tmp_path):
    store, _ = submitted_store(tmp_path)
    store.record_provider_result("cancel-saga-1", provider())
    snap = store.record_reconciliation(
        "cancel-saga-1",
        reconciliation(orders=(readback(remaining="0", matched="2", cancelled="8"),)),
    )
    assert snap.state is CancelSagaState.CANCEL_RECONCILED
    assert snap.exposure.matched_bet_ids == ("bet-1",)
    assert snap.exposure.executable_bet_ids == ()


def test_timeout_then_authoritative_reduction_is_adopted_without_replay(tmp_path):
    store, _ = submitted_store(tmp_path, single_intent(cancel_size=Decimal("3")))
    store.mark_unknown("cancel-saga-1", reason="timeout", observed_at=OBSERVED)
    snap = store.record_reconciliation(
        "cancel-saga-1",
        reconciliation(orders=(readback(remaining="5", cancelled="3"),)),
    )
    assert snap.state is CancelSagaState.CANCEL_RECONCILED
    assert snap.retry_disposition is CancelRetryDisposition.TERMINAL_NO_RETRY


def test_failure_plus_unchanged_complete_readback_proves_no_effect(tmp_path):
    store, _ = submitted_store(tmp_path)
    store.record_provider_result(
        "cancel-saga-1",
        provider(CancelProviderStatus.FAILURE, error_code="BET_ACTION_ERROR"),
    )
    snap = store.record_reconciliation(
        "cancel-saga-1",
        reconciliation(orders=(readback(remaining="8", cancelled="0"),)),
    )
    assert snap.state is CancelSagaState.NO_EFFECT_RECONCILED
    assert snap.exposure.executable_bet_ids == ("bet-1",)
    assert snap.retry_disposition is CancelRetryDisposition.TERMINAL_NO_RETRY


def test_success_plus_unchanged_complete_readback_is_conflict(tmp_path):
    store, _ = submitted_store(tmp_path)
    store.record_provider_result("cancel-saga-1", provider())
    snap = store.record_reconciliation(
        "cancel-saga-1",
        reconciliation(orders=(readback(remaining="8", cancelled="0"),)),
    )
    assert snap.state is CancelSagaState.CONFLICT
    assert snap.retry_disposition is CancelRetryDisposition.CONFLICT_REQUIRES_OPERATOR


def test_matching_only_remainder_reduction_never_fabricates_cancel_success(tmp_path):
    intent = single_intent(cancel_size=Decimal("3"))
    store, _ = submitted_store(tmp_path, intent)
    store.record_provider_result("cancel-saga-1", provider())
    snap = store.record_reconciliation(
        "cancel-saga-1",
        reconciliation(
            orders=(readback(remaining="5", matched="5", cancelled="0"),),
        ),
    )
    assert snap.state is CancelSagaState.CONFLICT
    assert snap.exposure.matched_bet_ids == ("bet-1",)
    assert snap.retry_disposition is CancelRetryDisposition.CONFLICT_REQUIRES_OPERATOR


def test_foreign_readback_identity_is_rejected_before_journal_mutation(tmp_path):
    store, _ = submitted_store(tmp_path)
    with pytest.raises(BetfairCancelSagaIdentityConflict, match="exact source identity"):
        store.record_reconciliation(
            "cancel-saga-1",
            reconciliation(orders=(readback(account="other"),)),
        )
    assert store.verify_integrity() == 2


def test_order_disappearance_requires_explicit_final_evidence():
    with pytest.raises(ValueError, match="final/cleared"):
        reconciliation(absent=("bet-1",), complete=True)
    evidence = reconciliation(
        absent=("bet-1",),
        complete=True,
        final_sha=SHA_C,
    )
    assert evidence.absent_bet_ids == ("bet-1",)


def test_opaque_final_digest_cannot_terminalize_disappeared_single_order(tmp_path):
    store, _ = submitted_store(tmp_path)
    snap = store.record_reconciliation(
        "cancel-saga-1",
        reconciliation(absent=("bet-1",), complete=True, final_sha=SHA_C),
    )
    assert snap.state is CancelSagaState.SUBMITTED_UNKNOWN
    assert snap.retry_disposition is CancelRetryDisposition.READBACK_REQUIRED
    assert snap.exposure.provider_verified is False
    assert snap.exposure.matched_bet_ids == ("bet-1",)
    assert snap.exposure.executable_bet_ids == ()
    assert snap.exposure.unresolved_bet_ids == ("bet-1",)
    assert snap.exposure.portfolio_reset_proven is False

    restarted = BetfairCancelSagaStore(store.path).snapshot("cancel-saga-1")
    assert restarted.state is CancelSagaState.SUBMITTED_UNKNOWN
    assert restarted.retry_disposition is CancelRetryDisposition.READBACK_REQUIRED
    assert restarted.exposure.provider_verified is False
    assert restarted.exposure.unresolved_bet_ids == ("bet-1",)


def test_provider_success_plus_opaque_disappearance_stays_unverified(tmp_path):
    store, _ = submitted_store(tmp_path)
    store.record_provider_result("cancel-saga-1", provider())
    snap = store.record_reconciliation(
        "cancel-saga-1",
        reconciliation(absent=("bet-1",), complete=True, final_sha=SHA_C),
    )
    assert snap.state is CancelSagaState.PROVIDER_RESULT_UNVERIFIED
    assert snap.retry_disposition is CancelRetryDisposition.READBACK_REQUIRED
    assert snap.exposure.provider_verified is False
    assert snap.exposure.unresolved_bet_ids == ("bet-1",)


def test_market_scope_complete_readback_with_no_executable_limit_orders_reconciles(tmp_path):
    store, value = submitted_store(tmp_path, market_intent())
    snap = store.record_reconciliation(
        value.saga_id,
        reconciliation(
            orders=(readback(bet="bet-a", remaining="0", cancelled="8"),),
            complete=True,
        ),
    )
    assert snap.state is CancelSagaState.CANCEL_RECONCILED
    assert snap.exposure.provider_verified is True


def test_market_scope_foreign_market_is_rejected(tmp_path):
    store, value = submitted_store(tmp_path, market_intent())
    with pytest.raises(BetfairCancelSagaIdentityConflict, match="foreign market"):
        store.record_reconciliation(
            value.saga_id,
            reconciliation(orders=(readback(market="9.999", bet="bet-x"),)),
        )


def test_account_scope_complete_readback_with_remaining_limit_stays_nonterminal(tmp_path):
    store, value = submitted_store(tmp_path, account_intent())
    snap = store.record_reconciliation(
        value.saga_id,
        reconciliation(
            orders=(readback(bet="bet-a", remaining="1", cancelled="7"),),
            complete=True,
        ),
    )
    assert snap.state is CancelSagaState.SUBMITTED_UNKNOWN
    assert snap.retry_disposition is CancelRetryDisposition.READBACK_REQUIRED
    assert snap.exposure.executable_bet_ids == ("bet-a",)


def test_duplicate_reconciliation_is_idempotent_but_conflicting_evidence_id_fails(tmp_path):
    store, _ = submitted_store(tmp_path)
    evidence = reconciliation(orders=(readback(),))
    first = store.record_reconciliation("cancel-saga-1", evidence)
    second = store.record_reconciliation("cancel-saga-1", evidence)
    assert first.state == second.state
    conflict = replace(evidence, orders=(readback(remaining="1", cancelled="7"),))
    with pytest.raises(BetfairCancelSagaIdentityConflict, match="evidence id"):
        store.record_reconciliation("cancel-saga-1", conflict)


def test_reconciliation_timestamp_must_advance(tmp_path):
    store, _ = submitted_store(tmp_path)
    store.record_reconciliation(
        "cancel-saga-1",
        reconciliation(
            orders=(readback(remaining="5", cancelled="3"),),
            complete=False,
        ),
    )
    with pytest.raises(BetfairCancelSagaStateError, match="advance"):
        store.record_reconciliation(
            "cancel-saga-1",
            reconciliation(
                orders=(readback(remaining="4", cancelled="4"),),
                complete=False,
                evidence_id="e" * 64,
                observed_at=RECONCILED,
            ),
        )


def test_torn_journal_fails_closed(tmp_path):
    store = BetfairCancelSagaStore(tmp_path / "cancel.jsonl")
    store.prepare(single_intent())
    with store.path.open("ab") as handle:
        handle.write(b'{"torn":true}')
    with pytest.raises(BetfairCancelSagaIntegrityError, match="torn"):
        store.verify_integrity()


def test_duplicate_json_key_fails_closed(tmp_path):
    store = BetfairCancelSagaStore(tmp_path / "cancel.jsonl")
    store.prepare(single_intent())
    lines = store.path.read_text(encoding="utf-8").splitlines()
    event = lines[0]
    malformed = event.replace(
        '"schema_version":1',
        '"schema_version":1,"schema_version":1',
        1,
    )
    store.path.write_text(malformed + "\n", encoding="utf-8")
    with pytest.raises(BetfairCancelSagaIntegrityError, match="duplicate JSON key"):
        store.verify_integrity()


def test_digest_tamper_fails_closed(tmp_path):
    store = BetfairCancelSagaStore(tmp_path / "cancel.jsonl")
    store.prepare(single_intent())
    line = json.loads(store.path.read_text(encoding="utf-8"))
    line["payload"]["intent"]["authority_ref"] = "forged"
    store.path.write_text(
        json.dumps(line, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(BetfairCancelSagaIntegrityError, match="digest mismatch"):
        store.verify_integrity()


def test_customer_ref_is_provider_dedupe_aid_not_retry_state(tmp_path):
    value = single_intent()
    store = BetfairCancelSagaStore(tmp_path / "cancel.jsonl")
    snap = store.prepare(value)
    assert len(value.customer_ref) == 32
    assert (
        snap.retry_disposition
        is CancelRetryDisposition.EXPLICIT_EXECUTION_POLICY_REQUIRED
    )
    store.mark_submitted(value.saga_id, submitted_at=SUBMITTED)
    assert (
        store.snapshot(value.saga_id).retry_disposition
        is CancelRetryDisposition.READBACK_REQUIRED
    )
