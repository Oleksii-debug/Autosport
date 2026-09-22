from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betfair_replace_saga import (
    BetfairReplaceSagaStateError,
    BetfairReplaceSagaStore,
    OriginalOrderState,
    ReplaceExposureTruth,
    ReplaceInstruction,
    ReplaceInstructionReconciliation,
    ReplaceInstructionResult,
    ReplaceIntent,
    ReplacePhaseStatus,
    ReplaceProviderEvidence,
    ReplaceReconciliationEvidence,
    ReplaceRetryDisposition,
    ReplaceSagaState,
)


PREPARED = "2026-09-21T09:00:00+00:00"
SUBMITTED = "2026-09-21T09:00:01+00:00"
OBSERVED = "2026-09-21T09:00:02+00:00"
RECONCILED = "2026-09-21T09:00:03+00:00"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _intent() -> ReplaceIntent:
    return ReplaceIntent(
        saga_id="replace-authority-falsifier",
        account_id="acct-1",
        environment="supervised-live",
        market_id="1.234567",
        authority_ref="owner-goal:goal-1:revision:7",
        authority_sha256=SHA_A,
        prepared_at=PREPARED,
        market_version=42,
        instructions=(
            ReplaceInstruction("old-bet-1", Decimal("2.50")),
            ReplaceInstruction("old-bet-2", Decimal("3.20")),
        ),
    )


def _provider_failure_evidence() -> ReplaceProviderEvidence:
    return ReplaceProviderEvidence(
        evidence_id=SHA_B,
        response_sha256=SHA_C,
        observed_at=OBSERVED,
        source="caller-built:shape-valid-provider-result",
        results=(
            ReplaceInstructionResult(
                "old-bet-1",
                ReplacePhaseStatus.SUCCESS,
                ReplacePhaseStatus.FAILURE,
                None,
            ),
            ReplaceInstructionResult(
                "old-bet-2",
                ReplacePhaseStatus.SUCCESS,
                ReplacePhaseStatus.FAILURE,
                None,
            ),
        ),
    )


def _reconciliation_evidence() -> ReplaceReconciliationEvidence:
    return ReplaceReconciliationEvidence(
        evidence_id=SHA_D,
        observed_at=RECONCILED,
        source="caller-built:shape-valid-readback",
        results=(
            ReplaceInstructionReconciliation(
                "old-bet-1",
                OriginalOrderState.NOT_EXECUTABLE,
                "new-bet-1",
                SHA_A,
            ),
            ReplaceInstructionReconciliation(
                "old-bet-2",
                OriginalOrderState.NOT_EXECUTABLE,
                "new-bet-2",
                SHA_B,
            ),
        ),
    )


def _prepared_store(tmp_path) -> BetfairReplaceSagaStore:
    store = BetfairReplaceSagaStore(tmp_path / "replace-sagas.jsonl")
    store.prepare(_intent())
    return store


def _submitted_store(tmp_path) -> BetfairReplaceSagaStore:
    store = _prepared_store(tmp_path)
    store.mark_submitted("replace-authority-falsifier", submitted_at=SUBMITTED)
    return store


def test_prepared_cannot_mint_unknown_without_durable_submitted_boundary(tmp_path):
    store = _prepared_store(tmp_path)

    with pytest.raises(BetfairReplaceSagaStateError, match="SUBMITTED"):
        store.mark_unknown(
            "replace-authority-falsifier",
            reason="caller says transport was ambiguous",
            observed_at=OBSERVED,
        )

    restarted = BetfairReplaceSagaStore(store.path).snapshot(
        "replace-authority-falsifier"
    )
    assert restarted.state is ReplaceSagaState.PREPARED
    assert (
        restarted.retry_disposition
        is ReplaceRetryDisposition.EXPLICIT_EXECUTION_POLICY_REQUIRED
    )


def test_prepared_cannot_mint_reconciliation_without_external_boundary(tmp_path):
    store = _prepared_store(tmp_path)

    with pytest.raises(BetfairReplaceSagaStateError, match="SUBMITTED"):
        store.record_reconciliation(
            "replace-authority-falsifier",
            _reconciliation_evidence(),
        )

    restarted = BetfairReplaceSagaStore(store.path).snapshot(
        "replace-authority-falsifier"
    )
    assert restarted.state is ReplaceSagaState.PREPARED


def test_positive_exposure_authority_flags_are_not_caller_constructible():
    with pytest.raises(TypeError):
        ReplaceExposureTruth(
            saga_id="forged",
            state=ReplaceSagaState.REPLACED,
            known_not_executable_original_bet_ids=("old-bet-1",),
            known_executable_original_bet_ids=(),
            known_replacement_bet_ids=("new-bet-1",),
            unresolved_original_bet_ids=(),
            unresolved_replacement_bet_ids_for=(),
            request_sha256=SHA_A,
            provider_verified=True,
            execution_admission_eligible=True,
            real_money_authorized=True,
        )


def test_caller_minted_provider_result_cannot_create_terminal_no_retry_truth(tmp_path):
    store = _submitted_store(tmp_path)

    try:
        snapshot = store.record_provider_result(
            "replace-authority-falsifier",
            _provider_failure_evidence(),
        )
    except BetfairReplaceSagaStateError:
        return

    assert snapshot.retry_disposition is ReplaceRetryDisposition.READBACK_REQUIRED
    assert snapshot.state in {
        ReplaceSagaState.SUBMITTED_UNKNOWN,
        ReplaceSagaState.UNKNOWN_PARTIAL,
        ReplaceSagaState.CANCEL_CONFIRMED,
    }
    assert snapshot.exposure.provider_verified is False
    assert snapshot.exposure.execution_admission_eligible is False
    assert snapshot.exposure.real_money_authorized is False


def test_caller_minted_reconciliation_cannot_create_terminal_replaced_truth(tmp_path):
    store = _submitted_store(tmp_path)
    store.mark_unknown(
        "replace-authority-falsifier",
        reason="provider response lost",
        observed_at=OBSERVED,
    )

    try:
        snapshot = store.record_reconciliation(
            "replace-authority-falsifier",
            _reconciliation_evidence(),
        )
    except BetfairReplaceSagaStateError:
        return

    assert snapshot.retry_disposition is ReplaceRetryDisposition.READBACK_REQUIRED
    assert snapshot.state in {
        ReplaceSagaState.SUBMITTED_UNKNOWN,
        ReplaceSagaState.UNKNOWN_PARTIAL,
        ReplaceSagaState.CANCEL_CONFIRMED,
    }
    assert snapshot.exposure.provider_verified is False
    assert snapshot.exposure.execution_admission_eligible is False
    assert snapshot.exposure.real_money_authorized is False
