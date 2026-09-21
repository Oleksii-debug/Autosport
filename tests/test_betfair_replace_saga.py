from __future__ import annotations

import json
from decimal import Decimal

import pytest

from autosport.betfair_replace_saga import (
    BetfairReplaceSagaIdentityConflict,
    BetfairReplaceSagaIntegrityError,
    BetfairReplaceSagaStateError,
    BetfairReplaceSagaStore,
    OriginalOrderState,
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


def intent(*, price2: str = "3.20") -> ReplaceIntent:
    return ReplaceIntent(
        saga_id="replace-saga-1",
        account_id="acct-1",
        environment="supervised-live",
        market_id="1.234567",
        authority_ref="owner-goal:goal-1:revision:7",
        authority_sha256=SHA_A,
        prepared_at=PREPARED,
        market_version=42,
        instructions=(
            ReplaceInstruction("old-bet-1", Decimal("2.50")),
            ReplaceInstruction("old-bet-2", Decimal(price2)),
        ),
    )


def provider_evidence(
    *,
    cancel1=ReplacePhaseStatus.SUCCESS,
    place1=ReplacePhaseStatus.SUCCESS,
    new1="new-bet-1",
    cancel2=ReplacePhaseStatus.SUCCESS,
    place2=ReplacePhaseStatus.SUCCESS,
    new2="new-bet-2",
) -> ReplaceProviderEvidence:
    return ReplaceProviderEvidence(
        evidence_id=SHA_B,
        response_sha256=SHA_C,
        observed_at=OBSERVED,
        source="betfair:replaceOrders:response",
        results=(
            ReplaceInstructionResult("old-bet-1", cancel1, place1, new1),
            ReplaceInstructionResult("old-bet-2", cancel2, place2, new2),
        ),
    )


def reconciliation(
    *,
    original1=OriginalOrderState.NOT_EXECUTABLE,
    new1="new-bet-1",
    original2=OriginalOrderState.NOT_EXECUTABLE,
    new2="new-bet-2",
    evidence_id=SHA_D,
    observed_at=RECONCILED,
) -> ReplaceReconciliationEvidence:
    return ReplaceReconciliationEvidence(
        evidence_id=evidence_id,
        observed_at=observed_at,
        source="betfair:listCurrentOrders+listClearedOrders",
        results=(
            ReplaceInstructionReconciliation(
                "old-bet-1", original1, new1, SHA_A if new1 else None
            ),
            ReplaceInstructionReconciliation(
                "old-bet-2", original2, new2, SHA_B if new2 else None
            ),
        ),
    )


def prepared_store(tmp_path):
    store = BetfairReplaceSagaStore(tmp_path / "replace-sagas.jsonl")
    store.prepare(intent())
    return store


def submitted_store(tmp_path):
    store = prepared_store(tmp_path)
    store.mark_submitted("replace-saga-1", submitted_at=SUBMITTED)
    return store


def test_request_projection_matches_public_replace_contract_and_has_no_fake_customer_order_ref():
    value = intent()
    request = value.provider_request()

    assert request["method"] == "SportsAPING/v1.0/replaceOrders"
    assert request["params"]["marketId"] == "1.234567"
    assert request["params"]["instructions"] == [
        {"betId": "old-bet-1", "newPrice": "2.5"},
        {"betId": "old-bet-2", "newPrice": "3.2"},
    ]
    assert len(request["params"]["customerRef"]) == 32
    assert request["params"]["async"] is False
    assert request["params"]["marketVersion"] == {"version": 42}
    assert all("customerOrderRef" not in item for item in request["params"]["instructions"])
    assert len(value.request_sha256) == 64


def test_prepare_is_restart_stable_and_does_not_imply_external_write(tmp_path):
    store = prepared_store(tmp_path)

    restarted = BetfairReplaceSagaStore(store.path)
    snapshot = restarted.snapshot("replace-saga-1")

    assert snapshot.state is ReplaceSagaState.PREPARED
    assert snapshot.retry_disposition is ReplaceRetryDisposition.EXPLICIT_EXECUTION_POLICY_REQUIRED
    assert snapshot.exposure.known_replacement_bet_ids == ()
    assert snapshot.exposure.provider_verified is False
    assert snapshot.exposure.execution_admission_eligible is False
    assert snapshot.exposure.real_money_authorized is False


def test_same_saga_id_with_changed_replacement_terms_is_rejected(tmp_path):
    store = prepared_store(tmp_path)

    with pytest.raises(BetfairReplaceSagaIdentityConflict, match="different immutable"):
        store.prepare(intent(price2="3.30"))


def test_submission_crash_restarts_unknown_and_requires_readback(tmp_path):
    store = submitted_store(tmp_path)

    restarted = BetfairReplaceSagaStore(store.path)
    snapshot = restarted.snapshot("replace-saga-1")

    assert snapshot.state is ReplaceSagaState.SUBMITTED_UNKNOWN
    assert snapshot.retry_disposition is ReplaceRetryDisposition.READBACK_REQUIRED
    assert set(snapshot.exposure.unresolved_original_bet_ids) == {
        "old-bet-1",
        "old-bet-2",
    }
    assert set(snapshot.exposure.unresolved_replacement_bet_ids_for) == {
        "old-bet-1",
        "old-bet-2",
    }


def test_explicit_transport_ambiguity_remains_unknown_across_restart(tmp_path):
    store = submitted_store(tmp_path)
    snapshot = store.mark_unknown(
        "replace-saga-1",
        reason="replaceOrders timeout",
        observed_at=OBSERVED,
    )

    assert snapshot.state is ReplaceSagaState.SUBMITTED_UNKNOWN
    assert snapshot.retry_disposition is ReplaceRetryDisposition.READBACK_REQUIRED
    assert BetfairReplaceSagaStore(store.path).snapshot("replace-saga-1").state is ReplaceSagaState.SUBMITTED_UNKNOWN


def test_cancel_success_new_place_failure_never_rolls_back_original_exposure(tmp_path):
    store = submitted_store(tmp_path)
    evidence = provider_evidence(
        place1=ReplacePhaseStatus.FAILURE,
        new1=None,
        place2=ReplacePhaseStatus.FAILURE,
        new2=None,
    )

    snapshot = store.record_provider_result("replace-saga-1", evidence)

    assert snapshot.state is ReplaceSagaState.REPLACE_CANCELLED_WITHOUT_REPLACEMENT
    assert set(snapshot.exposure.known_not_executable_original_bet_ids) == {
        "old-bet-1",
        "old-bet-2",
    }
    assert snapshot.exposure.known_replacement_bet_ids == ()
    assert snapshot.exposure.unresolved_original_bet_ids == ()
    assert snapshot.exposure.unresolved_replacement_bet_ids_for == ()
    assert snapshot.retry_disposition is ReplaceRetryDisposition.TERMINAL_NO_RETRY


def test_full_provider_success_records_new_ids_but_does_not_grant_execution_authority(tmp_path):
    store = submitted_store(tmp_path)

    snapshot = store.record_provider_result("replace-saga-1", provider_evidence())

    assert snapshot.state is ReplaceSagaState.REPLACED
    assert set(snapshot.exposure.known_replacement_bet_ids) == {
        "new-bet-1",
        "new-bet-2",
    }
    assert snapshot.exposure.provider_verified is False
    assert snapshot.exposure.real_money_authorized is False


def test_timeout_can_be_reconciled_to_exactly_one_replacement_per_instruction(tmp_path):
    store = submitted_store(tmp_path)
    store.mark_unknown("replace-saga-1", reason="response lost", observed_at=OBSERVED)

    snapshot = store.record_reconciliation("replace-saga-1", reconciliation())

    assert snapshot.state is ReplaceSagaState.REPLACED
    assert set(snapshot.exposure.known_not_executable_original_bet_ids) == {
        "old-bet-1",
        "old-bet-2",
    }
    assert set(snapshot.exposure.known_replacement_bet_ids) == {
        "new-bet-1",
        "new-bet-2",
    }
    assert snapshot.retry_disposition is ReplaceRetryDisposition.TERMINAL_NO_RETRY


def test_old_order_still_executable_while_new_order_exists_is_conflict(tmp_path):
    store = submitted_store(tmp_path)
    store.mark_unknown("replace-saga-1", reason="response lost", observed_at=OBSERVED)

    snapshot = store.record_reconciliation(
        "replace-saga-1",
        reconciliation(original1=OriginalOrderState.EXECUTABLE),
    )

    assert snapshot.state is ReplaceSagaState.CONFLICT
    assert snapshot.exposure.known_executable_original_bet_ids == ("old-bet-1",)
    assert "new-bet-1" in snapshot.exposure.known_replacement_bet_ids
    assert snapshot.retry_disposition is ReplaceRetryDisposition.CONFLICT_REQUIRES_OPERATOR


def test_mixed_provider_place_success_failure_is_conflict_because_batch_place_is_atomic(tmp_path):
    store = submitted_store(tmp_path)
    evidence = provider_evidence(
        place2=ReplacePhaseStatus.FAILURE,
        new2=None,
    )

    snapshot = store.record_provider_result("replace-saga-1", evidence)

    assert snapshot.state is ReplaceSagaState.CONFLICT


def test_intermediate_cancel_success_place_unknown_exposes_known_cancelled_not_target_state(tmp_path):
    store = submitted_store(tmp_path)
    evidence = provider_evidence(
        place1=ReplacePhaseStatus.UNKNOWN,
        new1=None,
        place2=ReplacePhaseStatus.UNKNOWN,
        new2=None,
    )

    snapshot = store.record_provider_result("replace-saga-1", evidence)

    assert snapshot.state is ReplaceSagaState.UNKNOWN_PARTIAL
    assert set(snapshot.exposure.known_not_executable_original_bet_ids) == {
        "old-bet-1",
        "old-bet-2",
    }
    assert snapshot.exposure.known_replacement_bet_ids == ()
    assert set(snapshot.exposure.unresolved_replacement_bet_ids_for) == {
        "old-bet-1",
        "old-bet-2",
    }
    assert snapshot.retry_disposition is ReplaceRetryDisposition.READBACK_REQUIRED


def test_provider_and_reconciliation_disagree_on_new_bet_identity_is_conflict(tmp_path):
    store = submitted_store(tmp_path)
    store.record_provider_result("replace-saga-1", provider_evidence())
    disagreeing = reconciliation(new1="different-new-bet-1")

    snapshot = store.record_reconciliation("replace-saga-1", disagreeing)

    assert snapshot.state is ReplaceSagaState.CONFLICT
    assert snapshot.retry_disposition is ReplaceRetryDisposition.CONFLICT_REQUIRES_OPERATOR


def test_provider_place_failure_but_reconciliation_finds_new_order_is_conflict(tmp_path):
    store = submitted_store(tmp_path)
    store.record_provider_result(
        "replace-saga-1",
        provider_evidence(
            place1=ReplacePhaseStatus.FAILURE,
            new1=None,
            place2=ReplacePhaseStatus.FAILURE,
            new2=None,
        ),
    )

    snapshot = store.record_reconciliation("replace-saga-1", reconciliation())

    assert snapshot.state is ReplaceSagaState.CONFLICT


def test_provider_result_requires_exact_prepared_bet_set(tmp_path):
    store = submitted_store(tmp_path)
    evidence = ReplaceProviderEvidence(
        evidence_id=SHA_B,
        response_sha256=SHA_C,
        observed_at=OBSERVED,
        source="provider",
        results=(
            ReplaceInstructionResult(
                "old-bet-1",
                ReplacePhaseStatus.SUCCESS,
                ReplacePhaseStatus.SUCCESS,
                "new-bet-1",
            ),
        ),
    )

    with pytest.raises(BetfairReplaceSagaIdentityConflict, match="exact prepared"):
        store.record_provider_result("replace-saga-1", evidence)


def test_reconciliation_new_bet_requires_explicit_linkage_evidence():
    with pytest.raises(ValueError, match="linkage_sha256"):
        ReplaceInstructionReconciliation(
            "old-bet-1",
            OriginalOrderState.NOT_EXECUTABLE,
            "new-bet-1",
            None,
        )


def test_reconciliation_evidence_id_is_idempotent_but_conflict_is_rejected(tmp_path):
    store = submitted_store(tmp_path)
    store.mark_unknown("replace-saga-1", reason="response lost", observed_at=OBSERVED)
    first = reconciliation()

    one = store.record_reconciliation("replace-saga-1", first)
    two = store.record_reconciliation("replace-saga-1", first)
    assert two.event_count == one.event_count

    changed = reconciliation(original1=OriginalOrderState.EXECUTABLE)
    with pytest.raises(BetfairReplaceSagaIdentityConflict, match="reused"):
        store.record_reconciliation("replace-saga-1", changed)


def test_provider_result_before_submission_is_rejected(tmp_path):
    store = prepared_store(tmp_path)

    with pytest.raises(BetfairReplaceSagaStateError, match="SUBMITTED"):
        store.record_provider_result("replace-saga-1", provider_evidence())


def test_journal_torn_write_fails_closed(tmp_path):
    store = prepared_store(tmp_path)
    store.path.write_bytes(store.path.read_bytes().rstrip(b"\n"))

    with pytest.raises(BetfairReplaceSagaIntegrityError, match="unterminated"):
        store.verify_integrity()


def test_journal_duplicate_json_key_fails_closed(tmp_path):
    store = prepared_store(tmp_path)
    line = store.path.read_text(encoding="utf-8").splitlines()[0]
    corrupted = line.replace('"sha256":', '"sha256":"' + "f" * 64 + '","sha256":', 1)
    store.path.write_text(corrupted + "\n", encoding="utf-8")

    with pytest.raises(BetfairReplaceSagaIntegrityError, match="duplicate JSON key"):
        store.verify_integrity()


def test_journal_digest_tamper_fails_closed(tmp_path):
    store = prepared_store(tmp_path)
    envelope = json.loads(store.path.read_text(encoding="utf-8"))
    envelope["event"]["payload"]["intent"]["market_id"] = "1.attacker"
    store.path.write_text(json.dumps(envelope, separators=(",", ":")) + "\n", encoding="utf-8")

    with pytest.raises(BetfairReplaceSagaIntegrityError, match="digest mismatch"):
        store.verify_integrity()


def test_reconciliation_must_advance_causal_boundary(tmp_path):
    store = submitted_store(tmp_path)
    too_old = reconciliation(observed_at=SUBMITTED)

    with pytest.raises(BetfairReplaceSagaStateError, match="newer"):
        store.record_reconciliation("replace-saga-1", too_old)
