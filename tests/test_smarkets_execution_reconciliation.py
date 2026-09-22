from __future__ import annotations

import hashlib
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


def _action(**changes: object) -> ExecutionAction:
    values = dict(
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
    values.update(changes)
    return ExecutionAction(**values)


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
        reference_id="reference-1",
        account_id="acct-1",
        event_id="event-7",
        market_id="market-9",
        contract_id="contract-11",
        side="buy",
        requested_price_units=4000,
        requested_quantity_units=250000,
        executed_quantity_units=250000,
        executed_avg_price_units=4000,
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
    expected_reference_id: str = "reference-1",
):
    return verify_smarkets_order_readback(
        action,
        profile,
        authority,
        readback,
        expected_reference_id=expected_reference_id,
    )


def test_full_fill_uses_provider_quantity_not_stake_as_completion_truth() -> None:
    effect = _verify(_action(), _profile(), _authority(), _readback())
    assert effect.status is AcknowledgementStatus.ACCEPTED
    assert effect.accepted_stake == Decimal("10")
    assert effect.accepted_liability == Decimal("10")
    assert effect.accepted_odds == Decimal("2.5")
    assert effect.executed_quantity_units == 250000
    assert effect.executed_avg_price_units == 4000
    assert len(effect.evidence_id) == 64


def test_best_price_execution_keeps_quantity_and_reduces_back_stake() -> None:
    effect = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(executed_avg_price_units=2500),
    )
    assert effect.status is AcknowledgementStatus.ACCEPTED
    assert effect.executed_quantity_units == 250000
    assert effect.accepted_odds == Decimal("4")
    assert effect.accepted_stake == Decimal("6.25")
    assert effect.accepted_liability == Decimal("6.25")


def test_partial_provider_quantity_maps_to_partial_canonical_effect() -> None:
    effect = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            executed_quantity_units=100000,
            executed_avg_price_units=4000,
            state=SmarketsOrderState.PARTIAL,
        ),
    )
    assert effect.status is AcknowledgementStatus.PARTIAL
    assert effect.accepted_stake == Decimal("4")
    assert effect.accepted_odds == Decimal("2.5")


def test_lay_best_price_can_raise_backer_stake_while_reducing_liability() -> None:
    action = _action(
        side="LAY",
        requested_odds=Decimal("5"),
        requested_stake=Decimal("1"),
    )
    effect = _verify(
        action,
        _profile(),
        _authority(),
        _readback(
            side="sell",
            requested_price_units=2000,
            requested_quantity_units=50000,
            executed_quantity_units=50000,
            executed_avg_price_units=2500,
        ),
    )
    assert effect.status is AcknowledgementStatus.ACCEPTED
    assert effect.accepted_odds == Decimal("4")
    assert effect.accepted_stake == Decimal("1.25")
    assert effect.accepted_liability == Decimal("3.75")


def test_published_percentage_mapping_preserves_display_tick() -> None:
    action = _action(
        requested_odds=Decimal("1.65"),
        requested_stake=Decimal("10"),
    )
    effect = _verify(
        action,
        _profile(),
        _authority(),
        _readback(
            requested_price_units=6061,
            requested_quantity_units=164989,
            executed_quantity_units=164989,
            executed_avg_price_units=6061,
        ),
    )
    assert effect.accepted_odds == Decimal("1.65")
    assert effect.accepted_stake == Decimal("9.99998329")
    assert effect.accepted_liability == Decimal("9.99998329")


def test_non_tick_average_price_projects_deterministically() -> None:
    effect = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(executed_avg_price_units=3334),
    )
    assert str(effect.accepted_odds).startswith("2.999400119976004799")
    assert effect.accepted_stake == Decimal("8.335")
    assert effect.accepted_liability == Decimal("8.335")


def test_cancelled_unexecuted_order_is_rejected_effect() -> None:
    effect = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            executed_quantity_units=0,
            executed_avg_price_units=None,
            state=SmarketsOrderState.CANCELLED,
        ),
    )
    assert effect.status is AcknowledgementStatus.REJECTED
    assert effect.accepted_odds is None
    assert effect.accepted_stake == Decimal("0")
    assert effect.accepted_liability == Decimal("0")


def test_open_unexecuted_order_remains_pending() -> None:
    with pytest.raises(SmarketsReconciliationPending):
        _verify(
            _action(),
            _profile(),
            _authority(),
            _readback(
                executed_quantity_units=0,
                executed_avg_price_units=None,
                state=SmarketsOrderState.OPEN,
            ),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_id", "acct-other"),
        ("event_id", "event-other"),
        ("market_id", "market-other"),
        ("contract_id", "contract-other"),
        ("side", "sell"),
        ("requested_price_units", 4001),
        ("requested_quantity_units", 249999),
    ],
)
def test_any_order_identity_or_request_drift_fails_closed(
    field: str, value: object
) -> None:
    with pytest.raises(SmarketsReconciliationError):
        _verify(
            _action(), _profile(), _authority(), _readback(**{field: value})
        )


def test_exact_durable_reference_id_is_mandatory() -> None:
    with pytest.raises(SmarketsReconciliationError, match="durable submission identity"):
        _verify(
            _action(),
            _profile(),
            _authority(),
            _readback(),
            expected_reference_id="different-reference",
        )


def test_readback_reference_id_itself_cannot_drift() -> None:
    with pytest.raises(SmarketsReconciliationError, match="durable submission identity"):
        _verify(
            _action(),
            _profile(),
            _authority(),
            _readback(reference_id="different-reference"),
        )


def test_requested_odds_must_be_a_published_smarkets_tick() -> None:
    action = _action(requested_odds=Decimal("6.1"))
    with pytest.raises(SmarketsReconciliationError, match="exchange ladder"):
        _verify(
            action,
            _profile(),
            _authority(),
            _readback(
                requested_price_units=1639,
                requested_quantity_units=610000,
            ),
        )


def test_requested_stake_below_one_provider_quantity_unit_fails_closed() -> None:
    action = _action(
        requested_odds=Decimal("2.5"),
        requested_stake=Decimal("0.00001"),
    )
    with pytest.raises(SmarketsReconciliationError, match="below one Smarkets quantity"):
        _verify(
            action,
            _profile(),
            _authority(),
            _readback(
                requested_quantity_units=1,
                executed_quantity_units=1,
            ),
        )


def test_buy_execution_cannot_be_worse_than_requested_price() -> None:
    with pytest.raises(SmarketsReconciliationError, match="buy execution is worse"):
        _verify(
            _action(),
            _profile(),
            _authority(),
            _readback(executed_avg_price_units=4001),
        )


def test_sell_execution_cannot_be_worse_than_requested_price() -> None:
    action = _action(
        side="LAY",
        requested_odds=Decimal("5"),
        requested_stake=Decimal("1"),
    )
    with pytest.raises(SmarketsReconciliationError, match="sell execution is worse"):
        _verify(
            action,
            _profile(),
            _authority(),
            _readback(
                side="sell",
                requested_price_units=2000,
                requested_quantity_units=50000,
                executed_quantity_units=50000,
                executed_avg_price_units=1999,
            ),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_price_units", 0),
        ("requested_price_units", 10000),
        ("requested_price_units", 4000.0),
        ("requested_quantity_units", 0),
        ("requested_quantity_units", True),
        ("executed_quantity_units", -1),
        ("executed_quantity_units", 1.0),
        ("executed_avg_price_units", 0),
        ("executed_avg_price_units", 10000),
        ("executed_avg_price_units", 4000.0),
    ],
)
def test_provider_native_numeric_fields_are_strict_integers(
    field: str, value: object
) -> None:
    with pytest.raises(SmarketsReconciliationError):
        _readback(**{field: value})


def test_executed_quantity_cannot_exceed_requested_quantity() -> None:
    with pytest.raises(SmarketsReconciliationError, match="exceeds"):
        _readback(executed_quantity_units=250001)


def test_executed_quantity_requires_average_price_and_vice_versa() -> None:
    with pytest.raises(SmarketsReconciliationError, match="requires"):
        _readback(executed_avg_price_units=None)
    with pytest.raises(SmarketsReconciliationError, match="must not claim"):
        _readback(
            executed_quantity_units=0,
            executed_avg_price_units=4000,
            state=SmarketsOrderState.CANCELLED,
        )


def test_state_and_quantity_must_agree() -> None:
    with pytest.raises(SmarketsReconciliationError, match="FILLED"):
        _readback(
            executed_quantity_units=100000,
            state=SmarketsOrderState.FILLED,
        )
    with pytest.raises(SmarketsReconciliationError, match="PARTIAL"):
        _readback(state=SmarketsOrderState.PARTIAL)
    with pytest.raises(SmarketsReconciliationError, match="OPEN"):
        _readback(state=SmarketsOrderState.OPEN)
    with pytest.raises(SmarketsReconciliationError, match="REJECTED"):
        _readback(state=SmarketsOrderState.REJECTED)


def test_readback_capability_is_required() -> None:
    with pytest.raises(SmarketsReconciliationError, match="bet_readback"):
        _verify(_action(), _profile(readback=False), _authority(), _readback())


def test_profile_must_bind_exact_smarkets_official_adapter_identity() -> None:
    wrong = replace(_profile(), adapter_id="some-other-adapter")
    with pytest.raises(SmarketsReconciliationError, match="adapter identity"):
        _verify(_action(), wrong, _authority(), _readback())


def test_expired_or_out_of_scope_authority_fails_closed() -> None:
    with pytest.raises(SmarketsReconciliationError, match="expired"):
        _verify(
            _action(),
            _profile(),
            _authority(expires_at="2026-09-22T12:09:59+00:00"),
            _readback(),
        )
    with pytest.raises(SmarketsReconciliationError, match="event"):
        _verify(
            _action(),
            _profile(),
            _authority(approved_event_ids=("event-other",)),
            _readback(),
        )


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


def test_non_execution_data_uses_are_outside_execution_reconciliation_authority() -> None:
    authority = _authority()
    for purpose in (
        SmarketsDataPurpose.DATA_HARVESTING,
        SmarketsDataPurpose.REDISTRIBUTION,
        SmarketsDataPurpose.BENCHMARKING,
    ):
        with pytest.raises(SmarketsReconciliationError, match="outside"):
            authority.require_purpose(purpose)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("data_harvesting_approved", "data harvesting"),
        ("benchmarking_approved", "benchmarking"),
        ("redistribution_approved", "provider entitlement"),
    ],
)
def test_non_execution_approval_flags_cannot_mint_api_permission(
    field: str, message: str
) -> None:
    with pytest.raises(SmarketsReconciliationError, match=message):
        _authority(**{field: True})


def test_rate_limit_evidence_blocks_for_provider_relative_reset_seconds() -> None:
    evidence = SmarketsRateLimitEvidence(
        observed_at="2026-09-22T12:00:00+00:00",
        reset_after_seconds=10,
        source_payload_sha256="d" * 64,
    )
    assert not evidence.retry_allowed("2026-09-22T12:00:09.999999+00:00")
    assert evidence.retry_allowed("2026-09-22T12:00:10+00:00")


@pytest.mark.parametrize(
    "changes",
    [
        {"reset_after_seconds": -1},
        {"reset_after_seconds": True},
        {"http_status": 200},
        {"error_type": "SOMETHING_ELSE"},
    ],
)
def test_rate_limit_evidence_requires_exact_429_contract(
    changes: dict[str, object],
) -> None:
    values = dict(
        observed_at="2026-09-22T12:00:00+00:00",
        reset_after_seconds=10,
        source_payload_sha256="d" * 64,
    )
    values.update(changes)
    with pytest.raises(SmarketsReconciliationError):
        SmarketsRateLimitEvidence(**values)


def test_journal_is_restart_verifiable_and_exact_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    effect = _verify(_action(), _profile(), _authority(), _readback())
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    journal.append(effect)
    journal.append(effect)
    records = SmarketsReconciliationJournal(path).verify()
    assert len(records) == 1
    assert records[0]["evidence_id"] == effect.evidence_id
    assert records[0]["reference_id"] == "reference-1"
    assert records[0]["executed_quantity_units"] == 250000


def test_journal_detects_tampering(tmp_path: Path) -> None:
    effect = _verify(_action(), _profile(), _authority(), _readback())
    path = tmp_path / "smarkets-reconciliation.jsonl"
    SmarketsReconciliationJournal(path).append(effect)
    row = json.loads(path.read_text(encoding="utf-8"))
    row["record"]["accepted_stake"] = "999"
    path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(SmarketsReconciliationError, match="digest mismatch"):
        SmarketsReconciliationJournal(path).verify()


def test_same_provider_order_cannot_move_to_another_action(tmp_path: Path) -> None:
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    journal.append(_verify(_action(), _profile(), _authority(), _readback()))

    other = _verify(
        _action(action_id="action-2"),
        _profile(),
        _authority(),
        _readback(reference_id="reference-2"),
        expected_reference_id="reference-2",
    )
    with pytest.raises(SmarketsReconciliationError, match="provider_order_id"):
        journal.append(other)


def test_same_reference_cannot_alias_another_provider_order(tmp_path: Path) -> None:
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    journal.append(_verify(_action(), _profile(), _authority(), _readback()))

    aliased = _verify(
        _action(action_id="action-2"),
        _profile(),
        _authority(),
        _readback(provider_order_id="order-200"),
    )
    with pytest.raises(SmarketsReconciliationError, match="reference_id"):
        journal.append(aliased)


def test_journal_allows_causal_partial_to_full_progression(tmp_path: Path) -> None:
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    partial = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            executed_quantity_units=100000,
            executed_avg_price_units=4000,
            state=SmarketsOrderState.PARTIAL,
        ),
    )
    full = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            executed_quantity_units=250000,
            executed_avg_price_units=3500,
            state=SmarketsOrderState.FILLED,
            observed_at="2026-09-22T12:16:00+00:00",
            source_payload_sha256="d" * 64,
        ),
    )
    journal.append(partial)
    journal.append(full)
    records = SmarketsReconciliationJournal(path).verify()
    assert [row["status"] for row in records] == ["PARTIAL", "ACCEPTED"]
    assert [row["executed_quantity_units"] for row in records] == [100000, 250000]
    assert [row["accepted_stake"] for row in records] == ["4", "8.75"]


def test_journal_rejects_execution_regression(tmp_path: Path) -> None:
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    journal.append(
        _verify(
            _action(),
            _profile(),
            _authority(),
            _readback(
                executed_quantity_units=100000,
                executed_avg_price_units=4000,
                state=SmarketsOrderState.PARTIAL,
            ),
        )
    )
    regressed = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            executed_quantity_units=90000,
            executed_avg_price_units=4000,
            state=SmarketsOrderState.PARTIAL,
            observed_at="2026-09-22T12:16:00+00:00",
            source_payload_sha256="d" * 64,
        ),
    )
    with pytest.raises(SmarketsReconciliationError, match="executed quantity regressed"):
        journal.append(regressed)


def test_journal_rejects_readback_time_regression(tmp_path: Path) -> None:
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    journal.append(
        _verify(
            _action(),
            _profile(),
            _authority(),
            _readback(
                executed_quantity_units=100000,
                executed_avg_price_units=4000,
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
            executed_quantity_units=150000,
            executed_avg_price_units=4000,
            state=SmarketsOrderState.PARTIAL,
            observed_at="2026-09-22T12:16:00+00:00",
            source_payload_sha256="d" * 64,
        ),
    )
    with pytest.raises(SmarketsReconciliationError, match="time regressed"):
        journal.append(older)


def test_rejected_order_cannot_later_mint_fill_in_same_journal(tmp_path: Path) -> None:
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    rejected = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            executed_quantity_units=0,
            executed_avg_price_units=None,
            state=SmarketsOrderState.CANCELLED,
        ),
    )
    journal.append(rejected)
    later_fill = _verify(
        _action(),
        _profile(),
        _authority(),
        _readback(
            executed_quantity_units=100000,
            executed_avg_price_units=4000,
            state=SmarketsOrderState.PARTIAL,
            observed_at="2026-09-22T12:16:00+00:00",
            source_payload_sha256="d" * 64,
        ),
    )
    with pytest.raises(SmarketsReconciliationError, match="rejected provider order"):
        journal.append(later_fill)


def test_restart_rejects_hash_correct_but_economically_forged_record(
    tmp_path: Path,
) -> None:
    path = tmp_path / "smarkets-reconciliation.jsonl"
    journal = SmarketsReconciliationJournal(path)
    journal.append(_verify(_action(), _profile(), _authority(), _readback()))

    row = json.loads(path.read_text(encoding="utf-8"))
    row["record"]["accepted_stake"] = "9"
    unsigned = {
        "prev_sha256": row["prev_sha256"],
        "record": row["record"],
        "schema_version": row["schema_version"],
    }
    encoded = json.dumps(
        unsigned,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    row["record_sha256"] = hashlib.sha256(encoded).hexdigest()
    path.write_text(
        json.dumps(
            row,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SmarketsReconciliationError, match="fixed-point economics"):
        SmarketsReconciliationJournal(path).verify()
