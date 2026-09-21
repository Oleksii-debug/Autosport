from decimal import Decimal
import json

import pytest

from autosport.bookmaker_account_reconciliation import (
    AccountReconciliationIntegrityError,
    AccountSnapshotStaleError,
    BookmakerAccountReconciliationStore,
    ReconciledPositionState,
    snapshot_fingerprint,
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


_T1 = "2026-09-21T08:00:00+00:00"
_T2 = "2026-09-21T08:01:00+00:00"
_T3 = "2026-09-21T08:02:00+00:00"
_HASH = "a" * 64


def _profile(
    *capabilities: BookmakerCapability,
    venue_id: str = "book-a",
    account_id: str = "acct-a",
    adapter_id: str = "adapter-a",
) -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id=venue_id,
        account_id=account_id,
        adapter_id=adapter_id,
        adapter_version="1",
        profile_version=1,
        facts=tuple(
            BookmakerCapabilityFact(
                capability=capability,
                state=BookmakerCapabilityState.SUPPORTED,
            )
            for capability in capabilities
        ),
        observed_at=_T1,
        source_ref="capability-probe",
        source_payload_sha256=_HASH,
    )


def _balance(
    observed_at: str,
    amount: str,
    *,
    observation_id: str,
    venue_id: str = "book-a",
    account_id: str = "acct-a",
    adapter_id: str = "adapter-a",
) -> BookmakerBalanceObservation:
    return BookmakerBalanceObservation(
        venue_id=venue_id,
        account_id=account_id,
        adapter_id=adapter_id,
        observation_id=observation_id,
        currency="EUR",
        available_balance=Decimal(amount),
        observed_at=observed_at,
        source_payload_sha256=_HASH,
    )


def _position(
    state: BookmakerPositionState,
    observed_at: str,
    external_position_id: str = "pos-1",
    *,
    observation_id: str | None = None,
    venue_id: str = "book-a",
    account_id: str = "acct-a",
    adapter_id: str = "adapter-a",
) -> BookmakerPositionObservation:
    return BookmakerPositionObservation(
        venue_id=venue_id,
        account_id=account_id,
        adapter_id=adapter_id,
        observation_id=observation_id or f"{external_position_id}-{state.value}-{observed_at}",
        external_position_id=external_position_id,
        state=state,
        currency="EUR",
        observed_at=observed_at,
        source_payload_sha256=_HASH,
        provider_amount=Decimal("10"),
        provider_amount_semantics="backer_stake",
        decimal_odds=Decimal("2.0"),
    )


def _snapshot(
    observed_at: str,
    *,
    capabilities: tuple[BookmakerCapability, ...] = (),
    balance: BookmakerBalanceObservation | None = None,
    open_positions: tuple[BookmakerPositionObservation, ...] = (),
    settled_positions: tuple[BookmakerPositionObservation, ...] = (),
    venue_id: str = "book-a",
    account_id: str = "acct-a",
    adapter_id: str = "adapter-a",
) -> BookmakerAccountSnapshot:
    return BookmakerAccountSnapshot(
        profile=_profile(
            *capabilities,
            venue_id=venue_id,
            account_id=account_id,
            adapter_id=adapter_id,
        ),
        observed_capabilities=frozenset(capabilities),
        observed_at=observed_at,
        balance=balance,
        open_positions=open_positions,
        settled_positions=settled_positions,
    )


def test_restart_preserves_open_then_incomplete_read_becomes_unknown(tmp_path) -> None:
    path = tmp_path / "account.json"
    first = _snapshot(
        _T1,
        capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,),
        open_positions=(_position(BookmakerPositionState.OPEN, _T1),),
    )
    second = _snapshot(_T2)

    store = BookmakerAccountReconciliationStore(path)
    assert store.append_snapshot(first) is True

    restarted = BookmakerAccountReconciliationStore(path)
    assert restarted.append_snapshot(second) is True
    state = restarted.latest_state()

    assert state is not None
    assert state.position_state("pos-1") is ReconciledPositionState.UNKNOWN


def test_complete_disappearance_is_not_silently_settled(tmp_path) -> None:
    store = BookmakerAccountReconciliationStore(tmp_path / "account.json")
    assert store.append_snapshot(
        _snapshot(
            _T1,
            capabilities=(
                BookmakerCapability.OPEN_POSITIONS_READ,
                BookmakerCapability.SETTLED_POSITIONS_READ,
            ),
            open_positions=(_position(BookmakerPositionState.OPEN, _T1),),
        )
    )
    assert store.append_snapshot(
        _snapshot(
            _T2,
            capabilities=(
                BookmakerCapability.OPEN_POSITIONS_READ,
                BookmakerCapability.SETTLED_POSITIONS_READ,
            ),
        )
    )

    state = store.latest_state()
    assert state is not None
    assert state.position_state("pos-1") is ReconciledPositionState.UNKNOWN


def test_open_becomes_settled_only_from_explicit_later_settled_observation(
    tmp_path,
) -> None:
    store = BookmakerAccountReconciliationStore(tmp_path / "account.json")
    assert store.append_snapshot(
        _snapshot(
            _T1,
            capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,),
            open_positions=(_position(BookmakerPositionState.OPEN, _T1),),
        )
    )
    assert store.append_snapshot(
        _snapshot(
            _T2,
            capabilities=(BookmakerCapability.SETTLED_POSITIONS_READ,),
            settled_positions=(
                _position(
                    BookmakerPositionState.SETTLED,
                    _T2,
                    observation_id="pos-1-settled",
                ),
            ),
        )
    )

    state = store.latest_state()
    assert state is not None
    assert state.position_state("pos-1") is ReconciledPositionState.SETTLED


def test_settled_position_cannot_regress_to_open(tmp_path) -> None:
    store = BookmakerAccountReconciliationStore(tmp_path / "account.json")
    assert store.append_snapshot(
        _snapshot(
            _T1,
            capabilities=(BookmakerCapability.SETTLED_POSITIONS_READ,),
            settled_positions=(
                _position(BookmakerPositionState.SETTLED, _T1),
            ),
        )
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="cannot regress to OPEN",
    ):
        store.append_snapshot(
            _snapshot(
                _T2,
                capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,),
                open_positions=(_position(BookmakerPositionState.OPEN, _T2),),
            )
        )


def test_stale_non_idempotent_snapshot_cannot_supersede_checkpoint(tmp_path) -> None:
    store = BookmakerAccountReconciliationStore(tmp_path / "account.json")
    assert store.append_snapshot(_snapshot(_T2))

    with pytest.raises(AccountSnapshotStaleError, match="older account snapshot"):
        store.append_snapshot(
            _snapshot(
                _T1,
                capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,),
                open_positions=(_position(BookmakerPositionState.OPEN, _T1),),
            )
        )


def test_exact_replay_is_idempotent_but_same_time_conflict_fails_closed(
    tmp_path,
) -> None:
    store = BookmakerAccountReconciliationStore(tmp_path / "account.json")
    first = _snapshot(_T1)
    assert store.append_snapshot(first) is True
    assert store.append_snapshot(first) is False

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="same observed_at",
    ):
        store.append_snapshot(
            _snapshot(
                _T1,
                capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,),
                open_positions=(_position(BookmakerPositionState.OPEN, _T1),),
            )
        )


def test_balance_change_is_exposed_only_as_unexplained_provider_delta(tmp_path) -> None:
    store = BookmakerAccountReconciliationStore(tmp_path / "account.json")
    assert store.append_snapshot(
        _snapshot(
            _T1,
            capabilities=(BookmakerCapability.BALANCE_READ,),
            balance=_balance(_T1, "100", observation_id="balance-1"),
        )
    )
    assert store.append_snapshot(
        _snapshot(
            _T2,
            capabilities=(BookmakerCapability.BALANCE_READ,),
            balance=_balance(_T2, "92.50", observation_id="balance-2"),
        )
    )

    state = store.latest_state()
    assert state is not None
    assert state.latest_balance_observation is not None
    assert state.latest_balance_observation.available_balance == Decimal("92.50")
    delta = state.unexplained_balance_delta
    assert delta is not None
    assert delta.amount == Decimal("-7.50")
    assert delta.previous_observation_id == "balance-1"
    assert delta.current_observation_id == "balance-2"


def test_snapshot_without_fresh_balance_does_not_republish_old_delta(tmp_path) -> None:
    store = BookmakerAccountReconciliationStore(tmp_path / "account.json")
    assert store.append_snapshot(
        _snapshot(
            _T1,
            capabilities=(BookmakerCapability.BALANCE_READ,),
            balance=_balance(_T1, "100", observation_id="balance-1"),
        )
    )
    assert store.append_snapshot(
        _snapshot(
            _T2,
            capabilities=(BookmakerCapability.BALANCE_READ,),
            balance=_balance(_T2, "90", observation_id="balance-2"),
        )
    )
    assert store.append_snapshot(_snapshot(_T3))

    state = store.latest_state()
    assert state is not None
    assert state.latest_balance_observation is not None
    assert state.latest_balance_observation.observation_id == "balance-2"
    assert state.unexplained_balance_delta is None


def test_store_rejects_mixed_account_identity(tmp_path) -> None:
    store = BookmakerAccountReconciliationStore(tmp_path / "account.json")
    assert store.append_snapshot(_snapshot(_T1))
    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="cannot mix venue/account/adapter",
    ):
        store.append_snapshot(_snapshot(_T2, account_id="acct-b"))


def test_persisted_snapshot_digest_tampering_is_detected_on_restart(tmp_path) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)
    assert store.append_snapshot(_snapshot(_T1))

    document = json.loads(path.read_text(encoding="utf-8"))
    document["snapshots"][0]["snapshot"]["observed_at"] = _T2
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="snapshot_id does not match",
    ):
        BookmakerAccountReconciliationStore(path).history()


def test_snapshot_fingerprint_is_deterministic_for_equivalent_tuple_order() -> None:
    capabilities = (BookmakerCapability.OPEN_POSITIONS_READ,)
    one = _position(
        BookmakerPositionState.OPEN,
        _T1,
        "pos-1",
        observation_id="obs-1",
    )
    two = _position(
        BookmakerPositionState.OPEN,
        _T1,
        "pos-2",
        observation_id="obs-2",
    )
    first = _snapshot(
        _T1,
        capabilities=capabilities,
        open_positions=(one, two),
    )
    second = _snapshot(
        _T1,
        capabilities=capabilities,
        open_positions=(two, one),
    )

    assert snapshot_fingerprint(first) == snapshot_fingerprint(second)
