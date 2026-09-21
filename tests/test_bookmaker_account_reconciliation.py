from decimal import Decimal, localcontext
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

@pytest.fixture(autouse=True)
def _isolated_monotonic_authority_root(tmp_path, monkeypatch) -> None:
    authority_root = (tmp_path.parent / f".{tmp_path.name}-monotonic-authority").resolve(
        strict=False
    )
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(authority_root))



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


def test_balance_observation_identity_cannot_be_reused_with_conflicting_content(
    tmp_path,
) -> None:
    store = BookmakerAccountReconciliationStore(tmp_path / "account.json")
    assert store.append_snapshot(
        _snapshot(
            _T1,
            capabilities=(BookmakerCapability.BALANCE_READ,),
            balance=_balance(
                _T1,
                "100",
                observation_id="balance-shared",
            ),
        )
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="balance observation_id was reused",
    ):
        store.append_snapshot(
            _snapshot(
                _T2,
                capabilities=(BookmakerCapability.BALANCE_READ,),
                balance=_balance(
                    _T2,
                    "90",
                    observation_id="balance-shared",
                ),
            )
        )


def test_position_observation_identity_cannot_be_reused_with_conflicting_content(
    tmp_path,
) -> None:
    store = BookmakerAccountReconciliationStore(tmp_path / "account.json")
    assert store.append_snapshot(
        _snapshot(
            _T1,
            capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,),
            open_positions=(
                _position(
                    BookmakerPositionState.OPEN,
                    _T1,
                    observation_id="position-shared",
                ),
            ),
        )
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="position observation_id was reused",
    ):
        store.append_snapshot(
            _snapshot(
                _T2,
                capabilities=(BookmakerCapability.SETTLED_POSITIONS_READ,),
                settled_positions=(
                    _position(
                        BookmakerPositionState.SETTLED,
                        _T2,
                        observation_id="position-shared",
                    ),
                ),
            )
        )


def test_identical_balance_observation_repeated_in_later_snapshot_is_not_a_new_delta(
    tmp_path,
) -> None:
    store = BookmakerAccountReconciliationStore(tmp_path / "account.json")
    balance = _balance(
        _T1,
        "100",
        observation_id="balance-same",
    )
    assert store.append_snapshot(
        _snapshot(
            _T1,
            capabilities=(BookmakerCapability.BALANCE_READ,),
            balance=balance,
        )
    )
    assert store.append_snapshot(
        _snapshot(
            _T2,
            capabilities=(BookmakerCapability.BALANCE_READ,),
            balance=balance,
        )
    )

    state = store.latest_state()
    assert state is not None
    assert state.latest_balance_observation == balance
    assert state.unexplained_balance_delta is None


def test_snapshot_fingerprint_is_independent_of_decimal_context() -> None:
    amount = "1234567890123456789012345678.9012345678901234567891"
    snapshot = _snapshot(
        _T1,
        capabilities=(BookmakerCapability.BALANCE_READ,),
        balance=_balance(_T1, amount, observation_id="balance-high-precision"),
    )

    with localcontext() as context:
        context.prec = 6
        low_precision_fingerprint = snapshot_fingerprint(snapshot)

    with localcontext() as context:
        context.prec = 80
        high_precision_fingerprint = snapshot_fingerprint(snapshot)

    assert low_precision_fingerprint == high_precision_fingerprint


def test_high_precision_decimal_persists_and_reopens_exactly_across_contexts(
    tmp_path,
) -> None:
    amount = "1234567890123456789012345678.9012345678901234567891"
    snapshot = _snapshot(
        _T1,
        capabilities=(BookmakerCapability.BALANCE_READ,),
        balance=_balance(_T1, amount, observation_id="balance-high-precision"),
    )
    path = tmp_path / "account.json"

    with localcontext() as context:
        context.prec = 6
        assert BookmakerAccountReconciliationStore(path).append_snapshot(snapshot) is True

    document = json.loads(path.read_text(encoding="utf-8"))
    persisted = document["snapshots"][0]["snapshot"]["balance"]["available_balance"]
    assert persisted == amount

    with localcontext() as context:
        context.prec = 80
        reopened = BookmakerAccountReconciliationStore(path).history()

    assert len(reopened) == 1
    assert reopened[0].balance is not None
    assert reopened[0].balance.available_balance == Decimal(amount)
    assert snapshot_fingerprint(reopened[0]) == snapshot_fingerprint(snapshot)


def test_high_precision_balance_delta_is_exact_across_decimal_contexts(
    tmp_path,
) -> None:
    previous_amount = "1234567890123456789012345678.9012345678901234567891"
    current_amount = "1234567890123456789012345678.9012345678901234567892"
    path = tmp_path / "account.json"

    with localcontext() as context:
        context.prec = 6
        store = BookmakerAccountReconciliationStore(path)
        assert store.append_snapshot(
            _snapshot(
                _T1,
                capabilities=(BookmakerCapability.BALANCE_READ,),
                balance=_balance(
                    _T1,
                    previous_amount,
                    observation_id="balance-delta-1",
                ),
            )
        )
        assert store.append_snapshot(
            _snapshot(
                _T2,
                capabilities=(BookmakerCapability.BALANCE_READ,),
                balance=_balance(
                    _T2,
                    current_amount,
                    observation_id="balance-delta-2",
                ),
            )
        )
        state = store.latest_state()

    assert state is not None
    assert state.unexplained_balance_delta is not None
    assert state.unexplained_balance_delta.amount == Decimal("1E-22")

    with localcontext() as context:
        context.prec = 80
        reopened_state = BookmakerAccountReconciliationStore(path).latest_state()

    assert reopened_state is not None
    assert reopened_state.unexplained_balance_delta is not None
    assert reopened_state.unexplained_balance_delta.amount == Decimal("1E-22")


def test_valid_old_file_rollback_cannot_resurrect_settled_position(tmp_path) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)
    assert store.append_snapshot(
        _snapshot(
            _T1,
            capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,),
            open_positions=(_position(BookmakerPositionState.OPEN, _T1),),
        )
    )
    old_valid_bytes = path.read_bytes()

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

    path.write_bytes(old_valid_bytes)

    with pytest.raises(AccountReconciliationIntegrityError, match="monotonic"):
        BookmakerAccountReconciliationStore(path).latest_state()


def test_valid_old_balance_checkpoint_cannot_erase_newer_balance(tmp_path) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)
    assert store.append_snapshot(
        _snapshot(
            _T1,
            capabilities=(BookmakerCapability.BALANCE_READ,),
            balance=_balance(_T1, "100", observation_id="balance-1"),
        )
    )
    old_valid_bytes = path.read_bytes()
    assert store.append_snapshot(
        _snapshot(
            _T2,
            capabilities=(BookmakerCapability.BALANCE_READ,),
            balance=_balance(_T2, "80", observation_id="balance-2"),
        )
    )

    path.write_bytes(old_valid_bytes)

    with pytest.raises(AccountReconciliationIntegrityError, match="monotonic"):
        BookmakerAccountReconciliationStore(path).latest_state()


def test_delete_after_history_cannot_pristine_rebootstrap(tmp_path) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)
    first = _snapshot(_T1)
    assert store.append_snapshot(first)
    path.unlink()

    restarted = BookmakerAccountReconciliationStore(path)
    with pytest.raises(AccountReconciliationIntegrityError, match="monotonic"):
        restarted.history()
    with pytest.raises(AccountReconciliationIntegrityError, match="monotonic"):
        restarted.append_snapshot(_snapshot(_T2))


def test_exact_current_checkpoint_reopens_idempotently(tmp_path) -> None:
    path = tmp_path / "account.json"
    first = _snapshot(
        _T1,
        capabilities=(BookmakerCapability.OPEN_POSITIONS_READ,),
        open_positions=(_position(BookmakerPositionState.OPEN, _T1),),
    )
    store = BookmakerAccountReconciliationStore(path)
    assert store.append_snapshot(first)

    restarted = BookmakerAccountReconciliationStore(path)
    assert restarted.history() == (first,)
    assert restarted.append_snapshot(first) is False


def test_schema_version_bool_is_not_integer_schema_version(tmp_path) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)
    assert store.append_snapshot(_snapshot(_T1))

    document = json.loads(path.read_text(encoding="utf-8"))
    document["schema_version"] = True
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(AccountReconciliationIntegrityError, match="schema_version"):
        BookmakerAccountReconciliationStore(path).history()


def test_crash_after_prepare_before_local_publish_aborts_safely(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)
    first = _snapshot(_T1)

    class SimulatedCrash(RuntimeError):
        pass

    def crash_before_publish(_encoded: bytes) -> None:
        raise SimulatedCrash("crash before local publish")

    monkeypatch.setattr(store, "_publish_history_bytes", crash_before_publish)
    with pytest.raises(SimulatedCrash, match="before local publish"):
        store.append_snapshot(first)

    assert not path.exists()
    restarted = BookmakerAccountReconciliationStore(path)
    assert restarted.history() == ()
    assert restarted.append_snapshot(first) is True
    assert restarted.history() == (first,)


def test_crash_after_local_publish_before_commit_recovers_exact_prepare(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)
    first = _snapshot(_T1)
    original_recover = store._recover_authority

    class SimulatedCrash(RuntimeError):
        pass

    def crash_before_commit(
        observed_state_sha256: str | None,
        *,
        history: list[BookmakerAccountSnapshot] | None = None,
    ) -> None:
        if observed_state_sha256 is not None:
            raise SimulatedCrash("crash after local publish")
        original_recover(observed_state_sha256, history=history)

    monkeypatch.setattr(store, "_recover_authority", crash_before_commit)
    with pytest.raises(SimulatedCrash, match="after local publish"):
        store.append_snapshot(first)

    assert path.exists()
    restarted = BookmakerAccountReconciliationStore(path)
    assert restarted.history() == (first,)
    assert restarted.append_snapshot(first) is False
