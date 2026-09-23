from decimal import Decimal

import pytest

import autosport.bookmaker_account_reconciliation as reconciliation_module
from autosport.bookmaker_account_reconciliation import (
    AccountReconciliationIntegrityError,
    BookmakerAccountReconciliationStore,
    snapshot_fingerprint,
    snapshot_to_canonical_dict,
)
from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority
from autosport.bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)


_OBSERVED_AT = "2026-09-23T00:00:00+00:00"
_HASH = "a" * 64


def _snapshot(
    amount: Decimal,
    *,
    observation_id: str = "balance-1",
    observed_at: str = _OBSERVED_AT,
) -> BookmakerAccountSnapshot:
    profile = BookmakerCapabilityProfile(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                capability=BookmakerCapability.BALANCE_READ,
                state=BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=observed_at,
        source_ref="bounded-decimal-regression",
        source_payload_sha256=_HASH,
    )
    balance = BookmakerBalanceObservation(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        observation_id=observation_id,
        currency="EUR",
        available_balance=amount,
        observed_at=observed_at,
        source_payload_sha256=_HASH,
    )
    return BookmakerAccountSnapshot(
        profile=profile,
        observed_capabilities=frozenset({BookmakerCapability.BALANCE_READ}),
        observed_at=observed_at,
        balance=balance,
    )


@pytest.mark.parametrize(
    "amount",
    (Decimal("1E+1000000"), Decimal("1E-1000000")),
)
def test_snapshot_fingerprint_rejects_extreme_fixed_point_materialization(
    amount: Decimal,
) -> None:
    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="canonical Decimal text exceeds bounded length",
    ):
        snapshot_fingerprint(_snapshot(amount))


def test_append_rejects_extreme_decimal_before_account_store_publication(tmp_path) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="canonical Decimal text exceeds bounded length",
    ):
        store.append_snapshot(_snapshot(Decimal("1E+1000000")))

    assert not path.exists()


def test_zero_with_extreme_exponent_canonicalizes_without_expansion() -> None:
    canonical = snapshot_to_canonical_dict(_snapshot(Decimal("0E+1000000")))

    assert canonical["balance"]["available_balance"] == "0"
    assert len(snapshot_fingerprint(_snapshot(Decimal("0E-1000000")))) == 64


@pytest.mark.parametrize(
    "zero_alias",
    (
        Decimal("0E+1000000"),
        Decimal("0E-1000000"),
        Decimal("-0E+1000000"),
        Decimal("-0E-1000000"),
    ),
)
def test_reconciliation_delta_treats_zero_exponent_as_scale_neutral(
    tmp_path,
    zero_alias: Decimal,
) -> None:
    store = BookmakerAccountReconciliationStore(tmp_path / "account.json")

    assert store.append_snapshot(
        _snapshot(
            Decimal("1"),
            observation_id="balance-1",
            observed_at="2026-09-23T00:00:00+00:00",
        )
    )
    assert store.append_snapshot(
        _snapshot(
            zero_alias,
            observation_id="balance-2",
            observed_at="2026-09-23T00:00:01+00:00",
        )
    )

    state = store.latest_state()
    assert state is not None
    assert state.unexplained_balance_delta is not None
    assert state.unexplained_balance_delta.amount == Decimal("-1")
    assert state.unexplained_balance_delta.previous_observation_id == "balance-1"
    assert state.unexplained_balance_delta.current_observation_id == "balance-2"


def test_authority_class_rebind_cannot_redirect_requested_root(
    monkeypatch,
    tmp_path,
) -> None:
    canonical_authority_class = MonotonicWorkspaceAuthority
    root_a = tmp_path / "authority-a"
    root_b = tmp_path / "authority-b"

    class RedirectedAuthority(canonical_authority_class):
        def __init__(self, *args, **kwargs):
            kwargs["authority_root"] = root_b
            super().__init__(*args, **kwargs)

    # The redirector deliberately preserves the exact canonical durability method
    # objects. Method/code seals alone therefore cannot distinguish this class.
    assert (
        RedirectedAuthority.read_history
        is canonical_authority_class.read_history
    )
    assert RedirectedAuthority.prepare is canonical_authority_class.prepare
    assert RedirectedAuthority.recover is canonical_authority_class.recover

    monkeypatch.setattr(
        reconciliation_module,
        "MonotonicWorkspaceAuthority",
        RedirectedAuthority,
    )
    path = tmp_path / "workspace" / "account.json"

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="monotonic authority class identity changed",
    ):
        BookmakerAccountReconciliationStore(
            path,
            authority_root=root_a,
        )

    assert not path.exists()
    assert not root_b.exists()


def test_genuine_authority_root_swap_is_rejected_before_publication(tmp_path) -> None:
    path = tmp_path / "workspace" / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=tmp_path / "authority-a",
    )
    original = store._authority

    store._authority = MonotonicWorkspaceAuthority(
        workspace=store._workspace,
        domain="provider.account-snapshot-reconciliation-v1",
        key=f"account-reconciliation:{path.name}",
        authority_root=tmp_path / "authority-b",
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="monotonic authority identity or binding changed",
    ):
        store.append_snapshot(_snapshot(Decimal("1")))

    assert store._authority is not original
    assert not path.exists()


@pytest.mark.parametrize("method_name", ("read_history", "prepare", "recover"))
def test_authority_instance_method_shadow_is_rejected_before_publication(
    tmp_path,
    method_name: str,
) -> None:
    path = tmp_path / "workspace" / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=tmp_path / "authority",
    )
    setattr(store._authority, method_name, lambda *args, **kwargs: None)

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="monotonic authority dispatch changed",
    ):
        store.append_snapshot(_snapshot(Decimal("1")))

    assert not path.exists()


def test_authority_binding_field_drift_is_rejected_before_publication(tmp_path) -> None:
    path = tmp_path / "workspace" / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=tmp_path / "authority",
    )
    store._authority.authority_root = tmp_path / "retargeted-authority"

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="monotonic authority identity or binding changed",
    ):
        store.append_snapshot(_snapshot(Decimal("1")))

    assert not path.exists()


def test_authority_guard_instance_shadow_cannot_redirect_requested_root(
    tmp_path,
) -> None:
    path = tmp_path / "workspace" / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=tmp_path / "authority-a",
    )
    replacement = MonotonicWorkspaceAuthority(
        workspace=store._workspace,
        domain="provider.account-snapshot-reconciliation-v1",
        key=f"account-reconciliation:{path.name}",
        authority_root=tmp_path / "authority-b",
    )

    # Reproduce the exact post-construction bypass: both the mutable authority
    # field and the instance-visible validator point at root B. Internal durable
    # paths must use the validator captured when the canonical class was defined,
    # so the closure-owned root-A issuance remains authoritative.
    store._authority = replacement
    store._require_canonical_authority = lambda: replacement

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="monotonic authority identity or binding changed",
    ):
        store.append_snapshot(_snapshot(Decimal("1")))

    assert not path.exists()




def test_caller_mutable_baseline_fields_cannot_authorize_alternate_root(
    tmp_path,
) -> None:
    path = tmp_path / "workspace" / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=tmp_path / "authority-a",
    )
    replacement = MonotonicWorkspaceAuthority(
        workspace=store._workspace,
        domain="provider.account-snapshot-reconciliation-v1",
        key=f"account-reconciliation:{path.name}",
        authority_root=tmp_path / "authority-b",
    )
    replacement_binding = (
        replacement.authority_root,
        replacement.workspace,
        replacement.workspace_instance_id,
        replacement.domain,
        replacement.key,
        replacement.namespace_sha256,
        replacement.journal_dir,
        replacement.namespace_marker_path,
        replacement.workspace_binding_path,
    )

    # This exactly reconstructs the predecessor bypass: all three caller-visible
    # fields agree on root B. The constructor-issued root-A baseline must live
    # outside mutable store state, so these decoy assignments grant no authority.
    store._authority = replacement
    store._authority_identity = replacement
    store._authority_binding = replacement_binding

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="monotonic authority identity or binding changed",
    ):
        store.append_snapshot(_snapshot(Decimal("1")))

    assert not path.exists()




def test_store_reinitialization_cannot_reissue_authority_root(tmp_path) -> None:
    path = tmp_path / "workspace" / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=tmp_path / "authority-a",
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="authority issuance is already registered",
    ):
        BookmakerAccountReconciliationStore.__init__(
            store,
            path,
            authority_root=tmp_path / "authority-b",
        )

    # __init__ assigned the attempted root-B authority before registration failed.
    # The original external root-A issuance still governs and rejects that partial
    # caller-driven reinitialization before any reconciliation state is published.
    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="monotonic authority identity or binding changed",
    ):
        store.append_snapshot(_snapshot(Decimal("1")))

    assert not path.exists()


def test_exact_ordinary_high_precision_decimal_remains_unrounded() -> None:
    amount = Decimal("1234567890.123456789012345678901234567890")
    canonical = snapshot_to_canonical_dict(_snapshot(amount))

    assert canonical["balance"]["available_balance"] == (
        "1234567890.12345678901234567890123456789"
    )


def test_fixed_point_materialization_boundary_is_deterministic() -> None:
    at_limit = snapshot_to_canonical_dict(_snapshot(Decimal("1E+4095")))
    assert len(at_limit["balance"]["available_balance"]) == 4096

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="canonical Decimal text exceeds bounded length",
    ):
        snapshot_to_canonical_dict(_snapshot(Decimal("1E+4096")))


def test_product_default_authority_root_ignores_environment_override(
    monkeypatch,
    tmp_path,
) -> None:
    baseline = reconciliation_module._product_account_reconciliation_authority_root()
    if reconciliation_module.os.name == "nt":
        assert baseline.parts[-3:] == (
            "Autosport",
            "application-state",
            "monotonic-authority-v1",
        )
    else:
        assert baseline.parts[-3:] == (
            "state",
            "autosport",
            "monotonic-authority-v1",
        )

    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(tmp_path / "caller-selected-root"),
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "caller-local-app-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "caller-xdg-state"))
    monkeypatch.setenv("HOME", str(tmp_path / "caller-home"))

    assert reconciliation_module._product_account_reconciliation_authority_root() == baseline


def test_default_authority_root_switch_cannot_pristine_rebootstrap_after_restart(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "workspace" / "account.json"
    product_root = tmp_path / "product-owned-authority"
    caller_root_a = tmp_path / "caller-root-a"
    caller_root_b = tmp_path / "caller-root-b"

    # Keep the test isolated from the real OS state directory while exercising
    # the production default-root branch (authority_root=None).
    monkeypatch.setattr(
        reconciliation_module,
        "_product_account_reconciliation_authority_root",
        lambda: product_root,
    )
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(caller_root_a))

    first = BookmakerAccountReconciliationStore(path)
    assert first.append_snapshot(_snapshot(Decimal("1")))
    assert first._authority.authority_root == product_root
    path.unlink()

    # Simulate a new process selecting a different caller/process override.
    # The supported default path must still re-open the same product-owned root,
    # where the committed high-water mark detects the deleted local state.
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(caller_root_b))
    restarted = BookmakerAccountReconciliationStore(path)
    assert restarted._authority.authority_root == product_root

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="failed independent monotonic authority validation",
    ):
        restarted.latest_snapshot()

    assert not path.exists()
    assert not caller_root_a.exists()
    assert not caller_root_b.exists()

