from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from autosport.betfair_replace_saga import (
    BetfairReplaceSagaIntegrityError,
    BetfairReplaceSagaStateError,
    BetfairReplaceSagaStore,
    OriginalOrderState,
    ReplaceInstruction,
    ReplaceInstructionReconciliation,
    ReplaceIntent,
    ReplaceReconciliationEvidence,
)


PREPARED = "2026-09-22T02:30:00+00:00"
SUBMITTED = "2026-09-22T02:30:01+00:00"
RECONCILED_1 = "2026-09-22T02:30:03+00:00"
RECONCILED_2 = "2026-09-22T02:30:04+00:00"
UNKNOWN_AFTER = "2026-09-22T02:30:05+00:00"
AUTHORITY_SHA = "a" * 64
LINKAGE_SHA = "b" * 64
SAGA_ID = "replace-replay-falsifier-1"


def _intent() -> ReplaceIntent:
    return ReplaceIntent(
        saga_id=SAGA_ID,
        account_id="acct-1",
        environment="supervised-live",
        market_id="1.234567",
        authority_ref="owner-goal:goal-1:revision:12",
        authority_sha256=AUTHORITY_SHA,
        prepared_at=PREPARED,
        market_version=42,
        instructions=(ReplaceInstruction("old-bet-1", Decimal("2.50")),),
    )


def _reconciliation(*, evidence_id: str, observed_at: str) -> ReplaceReconciliationEvidence:
    return ReplaceReconciliationEvidence(
        evidence_id=evidence_id,
        observed_at=observed_at,
        source="betfair:canonical-current+cleared-readback",
        results=(
            ReplaceInstructionReconciliation(
                bet_id="old-bet-1",
                original_state=OriginalOrderState.NOT_EXECUTABLE,
                new_bet_id=None,
                linkage_sha256=None,
            ),
        ),
    )


def _prepared_store(tmp_path) -> BetfairReplaceSagaStore:
    store = BetfairReplaceSagaStore(tmp_path / "replace-sagas.jsonl")
    store.prepare(_intent())
    return store


def _submitted_store(tmp_path) -> BetfairReplaceSagaStore:
    store = _prepared_store(tmp_path)
    store.mark_submitted(SAGA_ID, submitted_at=SUBMITTED)
    return store


def _rewrite_valid_hash_chain(store, mutate_events) -> None:
    envelopes = [
        json.loads(line)
        for line in store.path.read_text(encoding="utf-8").splitlines()
    ]
    events = [envelope["event"] for envelope in envelopes]
    mutate_events(events)

    previous = "0" * 64
    rewritten = []
    for event in events:
        event["prev_sha256"] = previous
        event_text = json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(event_text.encode("utf-8")).hexdigest()
        rewritten.append(
            json.dumps(
                {"event": event, "sha256": digest},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        previous = digest
    store.path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def test_prepared_event_saga_id_must_match_immutable_intent_identity(tmp_path):
    store = _prepared_store(tmp_path)

    def mutate(events):
        events[0]["saga_id"] = "forged-event-saga"

    _rewrite_valid_hash_chain(store, mutate)

    with pytest.raises(BetfairReplaceSagaIntegrityError):
        BetfairReplaceSagaStore(store.path).verify_integrity()


def test_prepared_envelope_recorded_at_must_match_intent_timestamp(tmp_path):
    store = _prepared_store(tmp_path)

    def mutate(events):
        events[0]["recorded_at"] = "2026-09-22T02:30:09.000000+00:00"

    _rewrite_valid_hash_chain(store, mutate)

    with pytest.raises(BetfairReplaceSagaIntegrityError):
        BetfairReplaceSagaStore(store.path).verify_integrity()


def test_submitted_envelope_recorded_at_must_match_submitted_payload(tmp_path):
    store = _submitted_store(tmp_path)

    def mutate(events):
        submitted = next(event for event in events if event["event_type"] == "SUBMITTED")
        submitted["recorded_at"] = "2026-09-22T02:30:09.000000+00:00"

    _rewrite_valid_hash_chain(store, mutate)

    with pytest.raises(BetfairReplaceSagaIntegrityError):
        BetfairReplaceSagaStore(store.path).verify_integrity()


def test_persisted_reconciliation_observed_at_must_strictly_advance(tmp_path):
    store = _submitted_store(tmp_path)
    store.record_reconciliation(
        SAGA_ID,
        _reconciliation(evidence_id="c" * 64, observed_at=RECONCILED_1),
    )
    store.record_reconciliation(
        SAGA_ID,
        _reconciliation(evidence_id="d" * 64, observed_at=RECONCILED_2),
    )

    def mutate(events):
        reconciliations = [
            event for event in events if event["event_type"] == "RECONCILIATION"
        ]
        regressed = "2026-09-22T02:30:02.500000+00:00"
        reconciliations[-1]["payload"]["evidence"]["observed_at"] = regressed
        reconciliations[-1]["recorded_at"] = regressed

    _rewrite_valid_hash_chain(store, mutate)

    with pytest.raises(BetfairReplaceSagaIntegrityError):
        BetfairReplaceSagaStore(store.path).verify_integrity()


def test_unknown_cannot_be_appended_after_reconciliation_for_same_replace_call(tmp_path):
    store = _submitted_store(tmp_path)
    store.record_reconciliation(
        SAGA_ID,
        _reconciliation(evidence_id="e" * 64, observed_at=RECONCILED_1),
    )

    with pytest.raises(BetfairReplaceSagaStateError):
        store.mark_unknown(
            SAGA_ID,
            reason="late ambiguity cannot precede already-durable readback",
            observed_at=UNKNOWN_AFTER,
        )
