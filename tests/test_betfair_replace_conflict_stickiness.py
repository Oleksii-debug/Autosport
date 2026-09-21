from __future__ import annotations

from decimal import Decimal

from autosport.betfair_replace_saga import (
    BetfairReplaceSagaStore,
    OriginalOrderState,
    ReplaceInstruction,
    ReplaceInstructionReconciliation,
    ReplaceIntent,
    ReplaceReconciliationEvidence,
    ReplaceRetryDisposition,
    ReplaceSagaState,
)

PREPARED = "2026-09-21T09:00:00+00:00"
SUBMITTED = "2026-09-21T09:00:01+00:00"
UNKNOWN = "2026-09-21T09:00:02+00:00"
CONFLICT_OBSERVED = "2026-09-21T09:00:03+00:00"
LATER_CLEAN_OBSERVED = "2026-09-21T09:00:04+00:00"
AUTHORITY_SHA = "a" * 64
CONFLICT_EVIDENCE_SHA = "b" * 64
CLEAN_EVIDENCE_SHA = "c" * 64
LINKAGE_SHA = "d" * 64


def _intent() -> ReplaceIntent:
    return ReplaceIntent(
        saga_id="replace-sticky-conflict-1",
        account_id="acct-1",
        environment="supervised-live",
        market_id="1.234567",
        authority_ref="owner-goal:goal-1:revision:7",
        authority_sha256=AUTHORITY_SHA,
        prepared_at=PREPARED,
        market_version=42,
        instructions=(
            ReplaceInstruction("old-bet-1", Decimal("2.50")),
        ),
    )


def _reconciliation(
    *,
    evidence_id: str,
    observed_at: str,
    original_state: OriginalOrderState,
) -> ReplaceReconciliationEvidence:
    return ReplaceReconciliationEvidence(
        evidence_id=evidence_id,
        observed_at=observed_at,
        source="betfair:canonical-current+cleared-readback",
        results=(
            ReplaceInstructionReconciliation(
                bet_id="old-bet-1",
                original_state=original_state,
                new_bet_id="new-bet-1",
                linkage_sha256=LINKAGE_SHA,
            ),
        ),
    )


def test_observed_dual_order_conflict_cannot_be_silently_cleared_by_later_clean_readback(
    tmp_path,
):
    """A provider-contract impossibility needs explicit resolution, not last-row wins.

    Betfair replaceOrders cancels the original before placing its replacement.  An
    authoritative readback that simultaneously observes the original as executable
    and a linked replacement therefore establishes a conflict/duplicate-exposure
    incident.  A later snapshot where the original is no longer executable may
    describe the *current* provider state, but it cannot retroactively prove that
    the prior conflict was harmless.  Without an explicit conflict-resolution
    authority/event, retry disposition must remain fail-closed across restart.
    """

    store = BetfairReplaceSagaStore(tmp_path / "replace-sagas.jsonl")
    store.prepare(_intent())
    store.mark_submitted("replace-sticky-conflict-1", submitted_at=SUBMITTED)
    store.mark_unknown(
        "replace-sticky-conflict-1",
        reason="replaceOrders timeout; canonical readback required",
        observed_at=UNKNOWN,
    )

    conflict = store.record_reconciliation(
        "replace-sticky-conflict-1",
        _reconciliation(
            evidence_id=CONFLICT_EVIDENCE_SHA,
            observed_at=CONFLICT_OBSERVED,
            original_state=OriginalOrderState.EXECUTABLE,
        ),
    )
    assert conflict.state is ReplaceSagaState.CONFLICT
    assert conflict.retry_disposition is ReplaceRetryDisposition.CONFLICT_REQUIRES_OPERATOR
    assert conflict.exposure.known_executable_original_bet_ids == ("old-bet-1",)
    assert conflict.exposure.known_replacement_bet_ids == ("new-bet-1",)

    later = store.record_reconciliation(
        "replace-sticky-conflict-1",
        _reconciliation(
            evidence_id=CLEAN_EVIDENCE_SHA,
            observed_at=LATER_CLEAN_OBSERVED,
            original_state=OriginalOrderState.NOT_EXECUTABLE,
        ),
    )

    # A later clean-looking snapshot is evidence about current state, not an
    # authority to erase the already-observed impossible overlap.  The current
    # #837 implementation incorrectly returns REPLACED + TERMINAL_NO_RETRY here.
    assert later.state is ReplaceSagaState.CONFLICT
    assert later.retry_disposition is ReplaceRetryDisposition.CONFLICT_REQUIRES_OPERATOR

    restarted = BetfairReplaceSagaStore(store.path).snapshot("replace-sticky-conflict-1")
    assert restarted.state is ReplaceSagaState.CONFLICT
    assert restarted.retry_disposition is ReplaceRetryDisposition.CONFLICT_REQUIRES_OPERATOR
