from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.bookmaker_account_reconciliation import (
    AccountPositionTransition,
    AccountReconciliationStatus,
    BookmakerAccountReconciliationError,
    reconcile_bookmaker_account_snapshots,
)
from autosport.bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    BookmakerPositionObservation,
    BookmakerPositionState,
)

READS = frozenset({
    BookmakerCapability.BALANCE_READ,
    BookmakerCapability.OPEN_POSITIONS_READ,
    BookmakerCapability.SETTLED_POSITIONS_READ,
})


def profile(
    *,
    version=1,
    account="acct-1",
    digest="1" * 64,
    adapter_version="1",
):
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id=account,
        adapter_id="betfair-readonly",
        adapter_version=adapter_version,
        profile_version=version,
        facts=tuple(
            BookmakerCapabilityFact(
                cap,
                BookmakerCapabilityState.SUPPORTED,
            )
            for cap in sorted(READS, key=lambda x: x.value)
        ),
        observed_at="2026-09-21T10:00:00+00:00",
        source_ref="test-profile",
        source_payload_sha256=digest,
    )


def balance(
    obs,
    amount,
    *,
    account="acct-1",
    currency="EUR",
):
    return BookmakerBalanceObservation(
        venue_id="betfair",
        account_id=account,
        adapter_id="betfair-readonly",
        observation_id=obs,
        currency=currency,
        available_balance=Decimal(amount),
        observed_at="2026-09-21T10:01:00+00:00",
        source_payload_sha256=(
            ("a" if obs.endswith("1") else "b") * 64
        ),
    )


def position(
    position_id,
    state,
    obs,
    *,
    account="acct-1",
    currency="EUR",
):
    return BookmakerPositionObservation(
        venue_id="betfair",
        account_id=account,
        adapter_id="betfair-readonly",
        observation_id=obs,
        external_position_id=position_id,
        state=state,
        currency=currency,
        observed_at="2026-09-21T10:01:00+00:00",
        source_payload_sha256=(
            ("c" if state is BookmakerPositionState.OPEN else "d") * 64
        ),
        provider_amount=Decimal("10"),
        provider_amount_semantics="backer_stake",
        provider_side="BACK",
        decimal_odds=Decimal("2.5"),
    )


def snapshot(
    *,
    at,
    prof=None,
    caps=READS,
    bal=None,
    opened=(),
    settled=(),
):
    prof = prof or profile()
    if (
        BookmakerCapability.BALANCE_READ in caps
        and bal is None
    ):
        bal = balance(
            "balance-1",
            "100",
            account=prof.account_id,
        )
    return BookmakerAccountSnapshot(
        profile=prof,
        observed_capabilities=caps,
        observed_at=at,
        balance=bal,
        open_positions=tuple(opened),
        settled_positions=tuple(settled),
    )


def test_clean_reconciliation_tracks_lifecycle_and_non_pnl_balance_delta():
    previous = snapshot(
        at="2026-09-21T10:02:00+00:00",
        bal=balance("balance-1", "100.00"),
        opened=(
            position(
                "bet-1",
                BookmakerPositionState.OPEN,
                "open-1",
            ),
            position(
                "bet-2",
                BookmakerPositionState.OPEN,
                "open-2",
            ),
        ),
    )
    current = snapshot(
        at="2026-09-21T10:03:00+00:00",
        bal=balance("balance-2", "112.50"),
        opened=(
            position(
                "bet-1",
                BookmakerPositionState.OPEN,
                "open-3",
            ),
        ),
        settled=(
            position(
                "bet-2",
                BookmakerPositionState.SETTLED,
                "settled-2",
            ),
        ),
    )

    report = reconcile_bookmaker_account_snapshots(
        previous,
        current,
    )

    assert (
        report.status
        is AccountReconciliationStatus.RECONCILED_DIAGNOSTIC
    )
    assert report.available_balance_delta == Decimal("12.50")
    assert not report.balance_delta_is_pnl
    assert not report.execution_authorized
    assert not report.settlement_authorized
    assert [
        row.transition
        for row in report.position_transitions
    ] == [
        AccountPositionTransition.OPEN_RETAINED,
        AccountPositionTransition.OPEN_TO_SETTLED,
    ]
    assert report.unresolved_external_position_ids == ()
    assert len(report.evidence_id) == 64


def test_disappearing_open_position_is_unresolved_not_assumed_settled():
    previous = snapshot(
        at="2026-09-21T10:02:00+00:00",
        opened=(
            position(
                "bet-1",
                BookmakerPositionState.OPEN,
                "open-1",
            ),
        ),
    )
    current = snapshot(
        at="2026-09-21T10:03:00+00:00"
    )

    report = reconcile_bookmaker_account_snapshots(
        previous,
        current,
    )

    assert (
        report.status
        is AccountReconciliationStatus.UNRESOLVED_POSITION_GAP
    )
    assert report.unresolved_external_position_ids == (
        "bet-1",
    )
    assert (
        report.position_transitions[0].transition
        is AccountPositionTransition.OPEN_DISAPPEARED_UNRESOLVED
    )


def test_missing_settled_read_is_incomplete_not_false_missing_effect():
    previous = snapshot(
        at="2026-09-21T10:02:00+00:00",
        opened=(
            position(
                "bet-1",
                BookmakerPositionState.OPEN,
                "open-1",
            ),
        ),
    )
    caps = frozenset({
        BookmakerCapability.BALANCE_READ,
        BookmakerCapability.OPEN_POSITIONS_READ,
    })
    current = snapshot(
        at="2026-09-21T10:03:00+00:00",
        caps=caps,
    )

    report = reconcile_bookmaker_account_snapshots(
        previous,
        current,
    )

    assert (
        report.status
        is AccountReconciliationStatus.INCOMPLETE_OBSERVATION
    )
    assert report.missing_capabilities == (
        "settled_positions_read",
    )
    assert report.position_transitions == ()
    assert report.unresolved_external_position_ids == ()


def test_identity_time_currency_and_profile_rollbacks_fail_closed():
    previous = snapshot(
        at="2026-09-21T10:02:00+00:00"
    )
    other = profile(account="acct-2")

    with pytest.raises(
        BookmakerAccountReconciliationError,
        match="identity mismatch",
    ):
        reconcile_bookmaker_account_snapshots(
            previous,
            snapshot(
                at="2026-09-21T10:03:00+00:00",
                prof=other,
                bal=balance(
                    "balance-2",
                    "100",
                    account="acct-2",
                ),
            ),
        )

    with pytest.raises(
        BookmakerAccountReconciliationError,
        match="precedes",
    ):
        reconcile_bookmaker_account_snapshots(
            snapshot(
                at="2026-09-21T10:03:00+00:00"
            ),
            snapshot(
                at="2026-09-21T10:02:00+00:00"
            ),
        )

    with pytest.raises(
        BookmakerAccountReconciliationError,
        match="rolled back",
    ):
        reconcile_bookmaker_account_snapshots(
            snapshot(
                at="2026-09-21T10:02:00+00:00",
                prof=profile(version=2),
            ),
            snapshot(
                at="2026-09-21T10:03:00+00:00",
                prof=profile(version=1),
            ),
        )

    with pytest.raises(
        BookmakerAccountReconciliationError,
        match="currency changed",
    ):
        reconcile_bookmaker_account_snapshots(
            snapshot(
                at="2026-09-21T10:02:00+00:00",
                bal=balance(
                    "balance-1",
                    "100",
                    currency="EUR",
                ),
            ),
            snapshot(
                at="2026-09-21T10:03:00+00:00",
                bal=balance(
                    "balance-2",
                    "100",
                    currency="GBP",
                ),
            ),
        )


def test_same_profile_version_can_be_reobserved_but_not_semantically_changed():
    previous = snapshot(
        at="2026-09-21T10:02:00+00:00",
        prof=profile(digest="1" * 64),
    )
    reobserved = snapshot(
        at="2026-09-21T10:03:00+00:00",
        prof=profile(digest="2" * 64),
    )
    assert (
        reconcile_bookmaker_account_snapshots(
            previous,
            reobserved,
        ).status
        is AccountReconciliationStatus.RECONCILED_DIAGNOSTIC
    )

    changed = snapshot(
        at="2026-09-21T10:03:00+00:00",
        prof=profile(
            digest="3" * 64,
            adapter_version="2",
        ),
    )
    with pytest.raises(
        BookmakerAccountReconciliationError,
        match="changed capability semantics",
    ):
        reconcile_bookmaker_account_snapshots(
            previous,
            changed,
        )


def test_settled_position_cannot_reappear_open():
    previous = snapshot(
        at="2026-09-21T10:02:00+00:00",
        settled=(
            position(
                "bet-1",
                BookmakerPositionState.SETTLED,
                "settled-1",
            ),
        ),
    )
    current = snapshot(
        at="2026-09-21T10:03:00+00:00",
        opened=(
            position(
                "bet-1",
                BookmakerPositionState.OPEN,
                "open-2",
            ),
        ),
    )

    with pytest.raises(
        BookmakerAccountReconciliationError,
        match="reappeared open",
    ):
        reconcile_bookmaker_account_snapshots(
            previous,
            current,
        )


def test_digest_and_transition_order_ignore_input_tuple_order():
    a = snapshot(
        at="2026-09-21T10:02:00+00:00",
        opened=(
            position(
                "bet-2",
                BookmakerPositionState.OPEN,
                "open-2",
            ),
            position(
                "bet-1",
                BookmakerPositionState.OPEN,
                "open-1",
            ),
        ),
    )
    b = replace(
        a,
        open_positions=tuple(reversed(a.open_positions)),
    )
    current = snapshot(
        at="2026-09-21T10:03:00+00:00",
        settled=(
            position(
                "bet-2",
                BookmakerPositionState.SETTLED,
                "settled-2",
            ),
            position(
                "bet-1",
                BookmakerPositionState.SETTLED,
                "settled-1",
            ),
        ),
    )

    ra = reconcile_bookmaker_account_snapshots(
        a,
        current,
    )
    rb = reconcile_bookmaker_account_snapshots(
        b,
        current,
    )

    assert ra.evidence_id == rb.evidence_id
    assert [
        row.external_position_id
        for row in ra.position_transitions
    ] == ["bet-1", "bet-2"]
