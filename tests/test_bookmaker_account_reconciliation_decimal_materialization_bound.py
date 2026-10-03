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


def _authority_root(tmp_path, name: str):
    # Monotonic authority must be outside the protected reconciliation workspace.
    return tmp_path.parent / f".{tmp_path.name}-{name}"


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
        authority_root=_authority_root(tmp_path, "authority-a"),
    )
    original = store._authority

    store._authority = MonotonicWorkspaceAuthority(
        workspace=store._workspace,
        domain="provider.account-snapshot-reconciliation-v1",
        key=f"account-reconciliation:{path.name}",
        authority_root=_authority_root(tmp_path, "authority-b"),
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
        authority_root=_authority_root(tmp_path, "authority"),
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
        authority_root=_authority_root(tmp_path, "authority"),
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
        authority_root=_authority_root(tmp_path, "authority-a"),
    )
    replacement = MonotonicWorkspaceAuthority(
        workspace=store._workspace,
        domain="provider.account-snapshot-reconciliation-v1",
        key=f"account-reconciliation:{path.name}",
        authority_root=_authority_root(tmp_path, "authority-b"),
    )

    # Reproduce the exact post-construction bypass: both the mutable authority
    # field and the instance-visible validator point at root B. Internal durable
    # paths must use the validator captured when the canonical class was defined,
    # so the closure-owned root-A issuance remains authoritative.
    store._authority = replacement
    store._require_canonical_authority = lambda: replacement

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="lower dispatch graph changed|monotonic authority identity or binding changed",
    ):
        store.append_snapshot(_snapshot(Decimal("1")))

    assert not path.exists()




def test_caller_mutable_baseline_fields_cannot_authorize_alternate_root(
    tmp_path,
) -> None:
    path = tmp_path / "workspace" / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=_authority_root(tmp_path, "authority-a"),
    )
    replacement = MonotonicWorkspaceAuthority(
        workspace=store._workspace,
        domain="provider.account-snapshot-reconciliation-v1",
        key=f"account-reconciliation:{path.name}",
        authority_root=_authority_root(tmp_path, "authority-b"),
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
        authority_root=_authority_root(tmp_path, "authority-a"),
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="authority issuance is already registered",
    ):
        BookmakerAccountReconciliationStore.__init__(
            store,
            path,
            authority_root=_authority_root(tmp_path, "authority-b"),
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

def test_snapshot_subclass_cannot_bypass_canonical_ingress_or_publish(tmp_path) -> None:
    canonical = _snapshot(Decimal("10"))

    class ForgedSnapshot(BookmakerAccountSnapshot):
        def __post_init__(self) -> None:
            # Deliberately bypass every canonical snapshot invariant.
            pass

    forged = ForgedSnapshot(
        profile=canonical.profile,
        observed_capabilities=frozenset(),
        observed_at=canonical.observed_at,
        balance=canonical.balance,
    )
    assert isinstance(forged, BookmakerAccountSnapshot)
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="exact canonical BookmakerAccountSnapshot",
    ):
        store.append_snapshot(forged)

    assert not path.exists()


def test_profile_subclass_cannot_enter_exact_snapshot_graph(tmp_path) -> None:
    canonical = _snapshot(Decimal("10"))

    class ForgedProfile(BookmakerCapabilityProfile):
        def __post_init__(self) -> None:
            # Empty venue would be rejected by the canonical profile constructor.
            pass

    forged_profile = ForgedProfile(
        venue_id="",
        account_id=canonical.profile.account_id,
        adapter_id=canonical.profile.adapter_id,
        adapter_version=canonical.profile.adapter_version,
        profile_version=canonical.profile.profile_version,
        facts=canonical.profile.facts,
        observed_at=canonical.profile.observed_at,
        source_ref=canonical.profile.source_ref,
        source_payload_sha256=canonical.profile.source_payload_sha256,
    )
    forged = BookmakerAccountSnapshot(
        profile=forged_profile,
        observed_capabilities=frozenset(),
        observed_at=canonical.observed_at,
    )
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="exact canonical BookmakerCapabilityProfile",
    ):
        store.append_snapshot(forged)

    assert not path.exists()


def test_balance_subclass_cannot_bypass_nested_evidence_validation(tmp_path) -> None:
    canonical = _snapshot(Decimal("10"))

    class ForgedBalance(BookmakerBalanceObservation):
        def __post_init__(self) -> None:
            # The non-SHA source identity is intentionally invalid canonical evidence.
            pass

    forged_balance = ForgedBalance(
        venue_id=canonical.balance.venue_id,
        account_id=canonical.balance.account_id,
        adapter_id=canonical.balance.adapter_id,
        observation_id=canonical.balance.observation_id,
        currency=canonical.balance.currency,
        available_balance=canonical.balance.available_balance,
        observed_at=canonical.balance.observed_at,
        source_payload_sha256="not-a-sha256",
    )
    forged = BookmakerAccountSnapshot(
        profile=canonical.profile,
        observed_capabilities=canonical.observed_capabilities,
        observed_at=canonical.observed_at,
        balance=forged_balance,
    )
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="exact canonical BookmakerBalanceObservation",
    ):
        store.append_snapshot(forged)

    assert not path.exists()

def test_exact_snapshot_mutation_is_revalidated_before_publication(tmp_path) -> None:
    forged = _snapshot(Decimal("10"))
    assert forged.balance is not None

    # Frozen dataclasses can still be altered through object.__setattr__. The
    # persistence boundary must validate current state, not only constructor history.
    object.__setattr__(forged.balance, "source_payload_sha256", "not-a-sha256")
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="failed current-state revalidation",
    ):
        store.append_snapshot(forged)

    assert not path.exists()


def test_exact_snapshot_cross_field_mutation_is_revalidated_before_publication(
    tmp_path,
) -> None:
    forged = _snapshot(Decimal("10"))
    object.__setattr__(forged, "observed_capabilities", frozenset())
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="failed current-state revalidation",
    ):
        store.append_snapshot(forged)

    assert not path.exists()


def test_module_snapshot_class_rebind_cannot_redefine_canonical_ingress(
    monkeypatch,
    tmp_path,
) -> None:
    canonical = _snapshot(Decimal("10"))

    class ForgedSnapshot(BookmakerAccountSnapshot):
        def __post_init__(self) -> None:
            # Same bypass shape as the predecessor subclass attack.
            pass

    forged = ForgedSnapshot(
        profile=canonical.profile,
        observed_capabilities=frozenset(),
        observed_at=canonical.observed_at,
        balance=canonical.balance,
    )
    assert isinstance(forged, BookmakerAccountSnapshot)

    # The imported class binding is part of the durable composition graph. A
    # runtime rebind must fail before the forged subclass can become decode/ingress
    # authority.
    monkeypatch.setattr(
        reconciliation_module,
        "BookmakerAccountSnapshot",
        ForgedSnapshot,
    )
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="canonical DTO class binding changed",
    ):
        store.append_snapshot(forged)

    assert not path.exists()


def test_runtime_snapshot_validator_rebind_cannot_suppress_revalidation(
    monkeypatch,
    tmp_path,
) -> None:
    forged = _snapshot(Decimal("10"))
    object.__setattr__(forged, "observed_capabilities", frozenset())

    # Preserve the exact canonical object type while replacing the live class
    # validator. Store admission must reject dispatch drift before traversing the
    # incoming DTO graph.
    monkeypatch.setattr(
        BookmakerAccountSnapshot,
        "__post_init__",
        lambda self: None,
    )
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(path)

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="canonical DTO class dispatch changed",
    ):
        store.append_snapshot(forged)

    assert not path.exists()


def test_runtime_profile_serializer_rebind_cannot_change_snapshot_fingerprint(
    monkeypatch,
) -> None:
    snapshot = _snapshot(Decimal("10"))
    baseline = snapshot_fingerprint(snapshot)

    monkeypatch.setattr(
        BookmakerCapabilityProfile,
        "to_canonical_dict",
        lambda self: {"forged": True},
    )

    assert snapshot_fingerprint(snapshot) == baseline


def test_reconciliation_read_rejects_preexisting_symlink_alias(tmp_path) -> None:
    target_workspace = tmp_path / "target-workspace"
    target_workspace.mkdir()
    target = target_workspace / "target.json"
    target_store = BookmakerAccountReconciliationStore(
        target,
        authority_root=_authority_root(tmp_path, "target-authority"),
    )
    assert target_store.append_snapshot(_snapshot(Decimal("10")))

    alias_workspace = tmp_path / "alias-workspace"
    alias_workspace.mkdir()
    alias = alias_workspace / "alias.json"
    try:
        alias.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable on this test runner")

    alias_store = BookmakerAccountReconciliationStore(
        alias,
        authority_root=_authority_root(tmp_path, "alias-authority"),
    )
    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="must be one regular file",
    ):
        alias_store.latest_snapshot()


def test_reconciliation_read_rejects_regular_file_swap_during_open(
    monkeypatch,
    tmp_path,
) -> None:
    primary_workspace = tmp_path / "primary-workspace"
    primary_workspace.mkdir()
    path = primary_workspace / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=_authority_root(tmp_path, "authority-a"),
    )
    assert store.append_snapshot(_snapshot(Decimal("10")))

    replacement_workspace = tmp_path / "replacement-workspace"
    replacement_workspace.mkdir()
    replacement = replacement_workspace / "replacement.json"
    replacement_store = BookmakerAccountReconciliationStore(
        replacement,
        authority_root=_authority_root(tmp_path, "authority-b"),
    )
    assert replacement_store.append_snapshot(
        _snapshot(
            Decimal("20"),
            observation_id="balance-replacement",
            observed_at="2026-09-23T00:00:01+00:00",
        )
    )

    canonical_open = reconciliation_module._open_read_only_descriptor
    swapped = False

    def swap_before_open(candidate):
        nonlocal swapped
        if not swapped and candidate == path:
            replacement.replace(path)
            swapped = True
        return canonical_open(candidate)

    monkeypatch.setattr(
        reconciliation_module,
        "_open_read_only_descriptor",
        swap_before_open,
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="changed during open",
    ):
        store.latest_snapshot()

    assert swapped is True


def test_stable_reconciliation_reader_returns_exact_persisted_bytes(tmp_path) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=_authority_root(tmp_path, "authority"),
    )
    assert store.append_snapshot(_snapshot(Decimal("10")))

    expected = path.read_bytes()
    assert reconciliation_module._read_stable_reconciliation_bytes(path) == expected


def test_reconciliation_read_normalizes_deep_json_recursion(tmp_path) -> None:
    path = tmp_path / "account.json"
    path.write_text(
        ("[" * 10_000) + ("]" * 10_000),
        encoding="utf-8",
    )
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=_authority_root(tmp_path, "authority"),
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="unreadable or corrupt",
    ):
        store.latest_snapshot()


def test_reconciliation_parent_alias_retarget_cannot_move_state_path(tmp_path) -> None:
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    alias = tmp_path / "workspace-link"
    try:
        alias.symlink_to(workspace_a, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlink creation is unavailable on this test runner")

    requested_path = alias / "account.json"
    store = BookmakerAccountReconciliationStore(
        requested_path,
        authority_root=_authority_root(tmp_path, "authority"),
    )
    canonical_path = workspace_a.resolve() / "account.json"
    assert store.path == canonical_path
    assert store._workspace == workspace_a.resolve()

    alias.unlink()
    alias.symlink_to(workspace_b, target_is_directory=True)

    assert store.append_snapshot(_snapshot(Decimal("10")))
    assert canonical_path.exists()
    assert not (workspace_b / "account.json").exists()
    assert store.latest_snapshot() == _snapshot(Decimal("10"))


@pytest.mark.parametrize(
    "method_name",
    ("_load_history", "_write_history", "_reconcile"),
)
def test_instance_lower_dispatch_shadow_cannot_bypass_durable_store(
    tmp_path,
    method_name: str,
) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=_authority_root(tmp_path, "authority"),
    )
    setattr(store, method_name, lambda *args, **kwargs: None)

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="lower dispatch graph changed",
    ):
        store.append_snapshot(_snapshot(Decimal("10")))

    assert not path.exists()


def test_class_lower_dispatch_rebind_cannot_bypass_durable_store(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=_authority_root(tmp_path, "authority"),
    )

    monkeypatch.setattr(
        BookmakerAccountReconciliationStore,
        "_write_history",
        lambda self, history: None,
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="lower dispatch graph changed",
    ):
        store.append_snapshot(_snapshot(Decimal("10")))

    assert not path.exists()


def test_module_stable_reader_rebind_cannot_forge_empty_history(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=_authority_root(tmp_path, "authority"),
    )
    expected = _snapshot(Decimal("10"))
    assert store.append_snapshot(expected)

    monkeypatch.setattr(
        reconciliation_module,
        "_read_stable_reconciliation_bytes",
        lambda candidate: None,
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="stable reader dispatch changed",
    ):
        store.latest_snapshot()

    assert path.exists()


@pytest.mark.parametrize(
    "binding_name",
    ("_decode_snapshot", "snapshot_fingerprint", "strict_json_loads"),
)
def test_dto_constructor_rebind_fails_before_durable_decode_callback(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=_authority_root(tmp_path, "authority"),
    )
    expected = _snapshot(Decimal("10"))
    assert store.append_snapshot(expected)

    canonical_init = BookmakerAccountSnapshot.__init__
    callback_reached = False

    def hostile_init(self, *args, **kwargs):
        nonlocal callback_reached
        callback_reached = True
        return canonical_init(self, *args, **kwargs)

    monkeypatch.setattr(BookmakerAccountSnapshot, "__init__", hostile_init)

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="canonical DTO class dispatch changed",
    ):
        store.latest_snapshot()

    assert callback_reached is False


def test_incoming_dto_field_descriptor_rebind_fails_before_callback(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=_authority_root(tmp_path, "authority"),
    )
    incoming = _snapshot(Decimal("10"))
    callback_reached = False

    class HostileProfileDescriptor:
        def __get__(self, instance, owner=None):
            nonlocal callback_reached
            callback_reached = True
            raise AssertionError("hostile profile descriptor executed")

        def __set__(self, instance, value):
            nonlocal callback_reached
            callback_reached = True
            raise AssertionError("hostile profile descriptor executed")

    monkeypatch.setattr(
        BookmakerAccountSnapshot,
        "profile",
        HostileProfileDescriptor(),
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="canonical DTO field descriptor changed",
    ):
        store.append_snapshot(incoming)

    assert callback_reached is False
    assert not path.exists()


def test_transitive_module_dispatch_rebind_fails_before_forged_read(
    monkeypatch,
    tmp_path,
    binding_name: str,
) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=_authority_root(tmp_path, "authority"),
    )
    expected = _snapshot(Decimal("10"))
    assert store.append_snapshot(expected)

    original = getattr(reconciliation_module, binding_name)
    callback_reached = False

    def hostile_dispatch(*args, **kwargs):
        nonlocal callback_reached
        callback_reached = True
        return original(*args, **kwargs)

    monkeypatch.setattr(reconciliation_module, binding_name, hostile_dispatch)

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="transitive module dispatch graph changed",
    ):
        store.latest_snapshot()

    assert callback_reached is False


def test_transitive_module_dispatch_rejects_authority_method_map_mutation(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=_authority_root(tmp_path, "authority"),
    )
    expected = _snapshot(Decimal("10"))
    assert store.append_snapshot(expected)

    callback_reached = False

    def forged_read_history(*args, **kwargs):
        nonlocal callback_reached
        callback_reached = True
        return ()

    monkeypatch.setitem(
        reconciliation_module._CANONICAL_AUTHORITY_METHODS,
        "read_history",
        forged_read_history,
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="transitive module dispatch mapping changed",
    ):
        store.latest_snapshot()

    assert callback_reached is False


def test_schema_version_runtime_rebind_cannot_reinterpret_durable_bytes(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=_authority_root(tmp_path, "authority"),
    )
    expected = _snapshot(Decimal("10"))
    assert store.append_snapshot(expected)

    monkeypatch.setattr(BookmakerAccountReconciliationStore, "SCHEMA_VERSION", 2)

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="class contract changed",
    ):
        store.latest_snapshot()


def test_stable_reader_default_rebind_cannot_forge_empty_history(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=_authority_root(tmp_path, "authority"),
    )
    expected = _snapshot(Decimal("10"))
    assert store.append_snapshot(expected)

    monkeypatch.setattr(
        BookmakerAccountReconciliationStore._load_history,
        "__kwdefaults__",
        {"_stable_read": lambda candidate: None},
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="lower dispatch implementation changed",
    ):
        store.latest_snapshot()

    assert path.exists()


def test_read_side_lower_dispatch_shadow_cannot_forge_empty_history(tmp_path) -> None:
    path = tmp_path / "account.json"
    store = BookmakerAccountReconciliationStore(
        path,
        authority_root=_authority_root(tmp_path, "authority"),
    )
    expected = _snapshot(Decimal("10"))
    assert store.append_snapshot(expected)

    store._load_history = lambda: []

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="lower dispatch graph changed",
    ):
        store.latest_snapshot()

    assert path.exists()
