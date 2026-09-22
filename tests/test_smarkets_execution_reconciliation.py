from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.real_execution_ledger import AcknowledgementStatus, ExecutionAction
from autosport.smarkets_execution_reconciliation import (
    SmarketsDataPurpose,
    SmarketsExecutionAuthority,
    SmarketsOrderReadback,
    SmarketsOrderState,
    SmarketsRateLimitEvidence,
    SmarketsReconciliationError,
    SmarketsReconciliationJournal,
    SmarketsReconciliationPending,
    verify_smarkets_order_readback,
)


def _profile(*, readback: bool = True) -> BookmakerCapabilityProfile:
    facts = [
        BookmakerCapabilityFact(
            BookmakerCapability.PLACE_BET, BookmakerCapabilityState.SUPPORTED
        )
    ]
    if readback:
        facts.append(
            BookmakerCapabilityFact(
                BookmakerCapability.BET_READBACK, BookmakerCapabilityState.SUPPORTED
            )
        )
    return BookmakerCapabilityProfile(
        venue_id="smarkets",
        account_id="acct-1",
        adapter_id="smarkets-official-api",
        adapter_version="1",
        profile_version=1,
        facts=tuple(facts),
        observed_at="2026-09-22T12:00:00+00:00",
        source_ref="official-smarkets-api-evidence",
        source_payload_sha256="a" * 64,
    )


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-1",
        bookmaker_id="smarkets",
        account_id="acct-1",
        event_id="event-7",
        market_id="market-9",
        selection_id="contract-11",
        side="BACK",
        requested_odds=Decimal("2.50"),
        requested_stake=Decimal("10.00"),
        quote_id="quote-1",
        quote_observed_at="2026-09-22T12:10:00+00:00",
        expires_at="2026-09-22T12:20:00+00:00",
    )


def _authority(**changes: object) -> SmarketsExecutionAuthority:
    values = dict(
        account_id="acct-1",
        approval_id="approval-1",
        approved_event_ids=("event-7",),
        approved_market_ids=("market-9",),
        observed_at="2026-09-22T12:00:00+00:00",
        expires_at="2026-09-22T13:00:00+00:00",
        source_ref="official-smarkets-account-approval",
        source_payload_sha256="b" * 64,
        execution_approved=True,
        data_harvesting_approved=False,
        redistribution_approved=False,
        benchmarking_approved=False,
    )
    values.update(changes)
    return SmarketsExecutionAuthority(**values)


def _readback(**changes: object) -> SmarketsOrderReadback:
    values = dict(
        provider_order_id="order-100",
        account_id="acct-1",
        event_id="event-7",
        market_id="market-9",
        contract_id="contract-11",
        side="BACK",
        requested_price=Decimal("2.50"),
        requested_quantity=Decimal("10.00"),
        matched_quantity=Decimal("10.00"),
        average_matched_price=Decimal("2.48"),
        state=SmarketsOrderState.FILLED,
        observed_at="2026-09-22T12:15:00+00:00",
        source_payload_sha256="c" * 64,
    )
    values.update(changes)
    return SmarketsOrderReadback(**values)


def _verify(
    action: ExecutionAction,
    profile: BookmakerCapabilityProfile,
    authority: SmarketsExecutionAuthority,
    readback: SmarketsOrderReadback,
    *,
    expected_provider_order_id: str = "order-100",
):
    return verify_smarkets_order_readback(
        action,
        profile,
        authority,
        readback,
        expected_provider_order_id=expected_provider_order_id,
    )


def test_full_fill_is_bound_to_exact_provider_readback() -> None:
    effect = _verify(
        _action(), _profile(), _authority(), _readback()
    )
    assert effect.status is AcknowledgementStatus.ACCEPTED
    assert effect.accepted_stake == Decimal("10.00")
    assert effect.accepted_odds == Decimal("2.48")
    assert len(effect.evidence_id) == 64


def test_partial_fill_is_partial_not_full_acceptance() -> None:
    effect = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            matched_quantity=Decimal("4.25"),
            average_matched_price=Decimal("2.49"),
            state=SmarketsOrderState.PARTIAL,
        ),
    )
    assert effect.status is AcknowledgementStatus.PARTIAL
    assert effect.accepted_stake == Decimal("4.25")


def test_cancelled_unmatched_order_is_rejected_effect() -> None:
    effect = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            matched_quantity=Decimal("0"),
            average_matched_price=None,
            state=SmarketsOrderState.CANCELLED,
        ),
    )
    assert effect.status is AcknowledgementStatus.REJECTED
    assert effect.accepted_odds is None
    assert effect.accepted_stake == Decimal("0")


def test_open_unmatched_order_remains_pending() -> None:
    with pytest.raises(SmarketsReconciliationPending):
        _verify(
            _action(),
            _profile(),
            _authority(),
            _readback(
                matched_quantity=Decimal("0"),
                average_matched_price=None,
                state=SmarketsOrderState.OPEN,
            ),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "acct-other"),
        ("event_id", "event-other"),
        ("market_id", "market-other"),
        ("contract_id", "contract-other"),
        ("side", "LAY"),
        ("requested_price", Decimal("2.51")),
        ("requested_quantity", Decimal("9.99")),
    ],
)
def test_any_order_identity_or_request_drift_fails_closed(field: str, value: object) -> None:
    with pytest.raises(SmarketsReconciliationError):
        _verify(
            _action(), _profile(), _authority(), _readback(**{field: value})
        )


def test_readback_capability_is_required() -> None:
    with pytest.raises(SmarketsReconciliationError, match="bet_readback"):
        _verify(
            _action(), _profile(readback=False), _authority(), _readback()
        )


def test_expired_or_out_of_scope_authority_fails_closed() -> None:
    with pytest.raises(SmarketsReconciliationError, match="expired"):
        _verify(
            _action(),
            _profile(),
            _authority(expires_at="2026-09-22T12:14:59+00:00"),
            _readback(),
        )
    with pytest.raises(SmarketsReconciliationError, match="event"):
        _verify(
            _action(),
            _profile(),
            _authority(approved_event_ids=("event-other",)),
            _readback(),
        )


def test_non_execution_data_uses_need_separate_explicit_approval() -> None:
    authority = _authority()
    for purpose in (
        SmarketsDataPurpose.DATA_HARVESTING,
        SmarketsDataPurpose.REDISTRIBUTION,
        SmarketsDataPurpose.BENCHMARKING,
    ):
        with pytest.raises(SmarketsReconciliationError):
            authority.require_purpose(purpose)
    _authority(data_harvesting_approved=True).require_purpose(
        SmarketsDataPurpose.DATA_HARVESTING
    )


def test_rate_limit_evidence_blocks_until_provider_reset() -> None:
    evidence = SmarketsRateLimitEvidence(
        observed_at="2026-09-22T12:00:00+00:00",
        provider_reset_at="2026-09-22T12:00:10+00:00",
        source_payload_sha256="d" * 64,
    )
    assert not evidence.retry_allowed("2026-09-22T12:00:09.999999+00:00")
    assert evidence.retry_allowed("2026-09-22T12:00:10+00:00")


def test_journal_is_restart_verifiable_and_exact_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    effect = _verify(
        _action(), _profile(), _authority(), _readback()
    )
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    journal.append(effect)
    journal.append(effect)
    restarted = SmarketsReconciliationJournal(path)
    records = restarted.verify()
    assert len(records) == 1
    assert records[0]["evidence_id"] == effect.evidence_id


def test_journal_detects_tampering(tmp_path: Path) -> None:
    effect = _verify(
        _action(), _profile(), _authority(), _readback()
    )
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    journal.append(effect)
    row = json.loads(path.read_text(encoding="utf-8"))
    row["record"]["accepted_stake"] = "999"
    path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(SmarketsReconciliationError, match="digest mismatch"):
        SmarketsReconciliationJournal(path).verify()


def test_same_provider_order_cannot_move_to_another_action(tmp_path: Path) -> None:
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    first = _verify(
        _action(), _profile(), _authority(), _readback()
    )
    journal.append(first)

    other_action = replace(_action(), action_id="action-2")
    second = _verify(
        other_action, _profile(), _authority(), _readback()
    )
    with pytest.raises(SmarketsReconciliationError, match="provider_order_id"):
        journal.append(second)


def test_exact_durable_provider_order_id_is_mandatory() -> None:
    with pytest.raises(SmarketsReconciliationError, match="durable submission identity"):
        _verify(
            _action(),
            _profile(),
            _authority(),
            _readback(),
            expected_provider_order_id="order-other",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_price", 2.5),
        ("requested_quantity", 10.0),
        ("matched_quantity", 10.0),
    ],
)
def test_binary_float_money_ingress_is_rejected(field: str, value: float) -> None:
    with pytest.raises(SmarketsReconciliationError, match="Decimal"):
        _readback(**{field: value})


def test_profile_must_bind_exact_smarkets_official_adapter_identity() -> None:
    wrong = replace(_profile(), adapter_id="some-other-adapter")
    with pytest.raises(SmarketsReconciliationError, match="adapter identity"):
        _verify(_action(), wrong, _authority(), _readback())


def test_post_hoc_execution_approval_cannot_authorize_earlier_action() -> None:
    with pytest.raises(SmarketsReconciliationError, match="not yet causally available"):
        _verify(
            _action(),
            _profile(),
            _authority(observed_at="2026-09-22T12:11:00+00:00"),
            _readback(),
        )


def test_later_readback_remains_valid_after_execution_approval_expiry() -> None:
    effect = _verify(
        _action(),
        _profile(),
        _authority(expires_at="2026-09-22T12:12:00+00:00"),
        _readback(observed_at="2026-09-22T12:30:00+00:00"),
    )
    assert effect.status is AcknowledgementStatus.ACCEPTED


def test_readback_cannot_predate_execution_action_evidence() -> None:
    with pytest.raises(SmarketsReconciliationError, match="cannot predate"):
        _verify(
            _action(),
            _profile(),
            _authority(),
            _readback(observed_at="2026-09-22T12:09:59+00:00"),
        )


def test_capability_profile_cannot_be_minted_after_action_time() -> None:
    late_profile = replace(
        _profile(), observed_at="2026-09-22T12:10:01+00:00"
    )
    with pytest.raises(SmarketsReconciliationError, match="capability profile"):
        _verify(_action(), late_profile, _authority(), _readback())


def test_journal_rejects_matched_stake_regression(tmp_path: Path) -> None:
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    first = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            matched_quantity=Decimal("4"),
            average_matched_price=Decimal("2.49"),
            state=SmarketsOrderState.PARTIAL,
            observed_at="2026-09-22T12:15:00+00:00",
        ),
    )
    journal.append(first)
    regressed = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            matched_quantity=Decimal("3"),
            average_matched_price=Decimal("2.49"),
            state=SmarketsOrderState.PARTIAL,
            observed_at="2026-09-22T12:16:00+00:00",
            source_payload_sha256="d" * 64,
        ),
    )
    with pytest.raises(SmarketsReconciliationError, match="stake regressed"):
        journal.append(regressed)


def test_journal_rejects_average_price_rewrite_without_new_fill(
    tmp_path: Path,
) -> None:
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    journal.append(
        _verify(
            _action(),
            _profile(),
            _authority(),
            _readback(
                matched_quantity=Decimal("4"),
                average_matched_price=Decimal("2.49"),
                state=SmarketsOrderState.PARTIAL,
            ),
        )
    )
    rewritten = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            matched_quantity=Decimal("4"),
            average_matched_price=Decimal("2.47"),
            state=SmarketsOrderState.PARTIAL,
            observed_at="2026-09-22T12:16:00+00:00",
            source_payload_sha256="d" * 64,
        ),
    )
    with pytest.raises(SmarketsReconciliationError, match="without a new fill"):
        journal.append(rewritten)


def test_journal_allows_causal_partial_to_full_progression(tmp_path: Path) -> None:
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    partial = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            matched_quantity=Decimal("4"),
            average_matched_price=Decimal("2.49"),
            state=SmarketsOrderState.PARTIAL,
        ),
    )
    full = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            matched_quantity=Decimal("10"),
            average_matched_price=Decimal("2.48"),
            state=SmarketsOrderState.FILLED,
            observed_at="2026-09-22T12:16:00+00:00",
            source_payload_sha256="d" * 64,
        ),
    )
    journal.append(partial)
    journal.append(full)
    records = SmarketsReconciliationJournal(path).verify()
    assert [row["status"] for row in records] == ["PARTIAL", "ACCEPTED"]
    assert [row["accepted_stake"] for row in records] == ["4", "10"]


def test_journal_rejects_readback_time_regression(tmp_path: Path) -> None:
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    journal.append(
        _verify(
            _action(),
            _profile(),
            _authority(),
            _readback(
                matched_quantity=Decimal("4"),
                average_matched_price=Decimal("2.49"),
                state=SmarketsOrderState.PARTIAL,
                observed_at="2026-09-22T12:17:00+00:00",
            ),
        )
    )
    older = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            matched_quantity=Decimal("5"),
            average_matched_price=Decimal("2.48"),
            state=SmarketsOrderState.PARTIAL,
            observed_at="2026-09-22T12:16:00+00:00",
            source_payload_sha256="d" * 64,
        ),
    )
    with pytest.raises(SmarketsReconciliationError, match="time regressed"):
        journal.append(older)
