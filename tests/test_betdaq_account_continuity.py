from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import autosport.betdaq_account_continuity as continuity_module
import autosport.betdaq_account_readonly as betdaq_account_module
from autosport.betdaq_account_continuity import (
    BetdaqAccountContinuityClient,
    BetdaqAccountContinuityError,
    BetdaqAccountContinuityMigrationRequiredError,
    BetdaqContinuousAccountEvidence,
    append_to_reconciliation,
    require_reconciliation_history_compatible,
)
from autosport.betdaq_account_readonly import (
    BetdaqAccountReadOnlyError,
    BetdaqCredentials,
)
from autosport.bookmaker_account_reconciliation import (
    AccountReconciliationIntegrityError,
    BookmakerAccountReconciliationStore,
)
from autosport.bookmaker_capability import BookmakerCapability


NS = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


class _FakeHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class QueueUrlopen:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        if not self.results:
            raise AssertionError("unexpected urlopen call")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return _FakeHttpResponse(result)


def soap(method, result_attributes="", inner="", return_status_code="0"):
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{SOAP}" xmlns="{NS}">'
        f"<soap:Body><{method}Response><{method}Result {result_attributes}>"
        f'<ReturnStatus Code="{return_status_code}" Description="fixture-status" '
        f'CallId="fixture-call" />'
        f"{inner}</{method}Result></{method}Response></soap:Body></soap:Envelope>"
    ).encode()


def balance(
    *,
    currency="EUR",
    available="100.01",
    total="120.02",
    exposure="-20.01",
    credit="0",
):
    return soap(
        "GetAccountBalances",
        (
            f'Currency="{currency}" Balance="{total}" Exposure="{exposure}" '
            f'AvailableFunds="{available}" Credit="{credit}"'
        ),
    )


def at(hour, minute):
    return lambda: datetime(2026, 9, 23, hour, minute, tzinfo=timezone.utc)


def acquire_balance(
    monkeypatch,
    credentials,
    *,
    payload=None,
    clock=None,
    account_id="caller-label",
    venue_id="betdaq",
):
    opener = QueueUrlopen(payload or balance())
    monkeypatch.setattr(betdaq_account_module, "urlopen", opener)
    client = BetdaqAccountContinuityClient(
        credentials,
        account_id=account_id,
        venue_id=venue_id,
        clock=clock or at(0, 0),
    )
    evidence = client.read_account_evidence(
        frozenset({BookmakerCapability.BALANCE_READ})
    )
    return evidence, opener


def test_same_authenticated_username_survives_password_and_application_rotation(
    monkeypatch,
):
    first, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "old-password", "old-app"),
        clock=at(0, 1),
    )
    second, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "new-password", "new-app"),
        payload=balance(available="99.99"),
        clock=at(0, 2),
    )

    assert (
        first.principal_context.principal_context_id
        == second.principal_context.principal_context_id
    )
    assert first.snapshot.profile.account_id == second.snapshot.profile.account_id
    assert (
        first.source_evidence.account_context.session_context_id
        != second.source_evidence.account_context.session_context_id
    )
    assert first.principal_context.authenticated_principal_continuity_proven is True
    assert first.principal_context.immutable_physical_account_identity_proven is False
    assert first.source_evidence.account_context.stable_account_identity_proven is False


def test_different_authenticated_usernames_never_collapse(monkeypatch):
    first, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice-a", "password-a", "app"),
        clock=at(0, 1),
    )
    second, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice-b", "password-b", "app"),
        clock=at(0, 2),
    )

    assert (
        first.principal_context.principal_context_id
        != second.principal_context.principal_context_id
    )
    assert first.snapshot.profile.account_id != second.snapshot.profile.account_id


def test_caller_cannot_fork_continuity_identity_with_custom_venue(monkeypatch):
    credentials = BetdaqCredentials("alice", "password", "app")

    with pytest.raises(
        BetdaqAccountContinuityError,
        match="venue_id is product-owned and must be canonical",
    ):
        acquire_balance(
            monkeypatch,
            credentials,
            venue_id="caller-fork",
            clock=at(0, 1),
        )


def test_caller_account_label_and_credentials_do_not_enter_continuity_identity(
    monkeypatch,
):
    credentials = BetdaqCredentials(
        "secret-user",
        "secret-password",
        "secret-application",
    )
    evidence, _ = acquire_balance(
        monkeypatch,
        credentials,
        clock=at(0, 1),
        account_id="friendly-caller-label",
    )

    public_text = " ".join(
        (
            repr(evidence.principal_context),
            evidence.principal_context.principal_context_id,
            evidence.snapshot.profile.account_id,
            evidence.snapshot.profile.source_ref,
        )
    )
    for secret in (
        "secret-user",
        "secret-password",
        "secret-application",
        "friendly-caller-label",
    ):
        assert secret not in public_text


def test_mid_acquisition_credential_rotation_still_fails_closed(monkeypatch):
    credentials = BetdaqCredentials("alice", "before-password", "app")

    class MutatingUrlopen(QueueUrlopen):
        def __call__(self, request, *, timeout):
            response = super().__call__(request, timeout=timeout)
            object.__setattr__(credentials, "password", "after-password")
            return response

    opener = MutatingUrlopen(balance())
    monkeypatch.setattr(betdaq_account_module, "urlopen", opener)
    client = BetdaqAccountContinuityClient(credentials, clock=at(0, 1))

    with pytest.raises(
        BetdaqAccountReadOnlyError,
        match="authenticated account context changed during acquisition",
    ):
        client.read_account_evidence(
            frozenset({BookmakerCapability.BALANCE_READ})
        )


def test_external_credentials_aba_mutation_cannot_mix_secure_requests(
    monkeypatch,
):
    credentials = BetdaqCredentials("alice", "password-a", "app-a")

    class AbaUrlopen(QueueUrlopen):
        def __call__(self, request, *, timeout):
            response = super().__call__(request, timeout=timeout)
            if len(self.calls) == 1:
                object.__setattr__(credentials, "username", "mallory")
                object.__setattr__(credentials, "password", "password-b")
                object.__setattr__(credentials, "application_identifier", "app-b")
            elif len(self.calls) == 2:
                object.__setattr__(credentials, "username", "alice")
                object.__setattr__(credentials, "password", "password-a")
                object.__setattr__(credentials, "application_identifier", "app-a")
            return response

    opener = AbaUrlopen(
        balance(),
        soap(
            "ListBootstrapOrders",
            'MaximumSequenceNumber="-1"',
            "<Orders />",
        ),
        soap("ListOrdersChangedSince", inner="<Orders />"),
    )
    monkeypatch.setattr(betdaq_account_module, "urlopen", opener)
    client = BetdaqAccountContinuityClient(credentials, clock=at(0, 1))

    evidence = client.read_account_evidence(
        frozenset(
            {
                BookmakerCapability.BALANCE_READ,
                BookmakerCapability.OPEN_POSITIONS_READ,
            }
        )
    )

    assert credentials == BetdaqCredentials("alice", "password-a", "app-a")
    assert len(opener.calls) == 3
    for request, _timeout in opener.calls:
        body = request.data
        assert b'username="alice"' in body
        assert b'password="password-a"' in body
        assert b'applicationIdentifier="app-a"' in body
        assert b"mallory" not in body
        assert b"password-b" not in body
        assert b"app-b" not in body
    assert evidence.snapshot.profile.account_id.startswith(
        "betdaq-authenticated-principal:"
    )


def test_continuous_identity_composes_with_reconciliation_across_new_clients(
    monkeypatch,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    store_path = workspace / "betdaq-account.json"

    first, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password-one", "app-one"),
        payload=balance(available="100.01"),
        clock=at(0, 1),
    )
    first_store = BookmakerAccountReconciliationStore(
        store_path,
        authority_root=authority,
    )
    assert append_to_reconciliation(first_store, first) is True

    second, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password-two", "app-two"),
        payload=balance(available="99.99"),
        clock=at(0, 2),
    )
    restarted_store = BookmakerAccountReconciliationStore(
        store_path,
        authority_root=authority,
    )
    assert append_to_reconciliation(restarted_store, second) is True

    state = restarted_store.latest_state()
    assert state is not None
    assert state.account_id == first.principal_context.principal_context_id
    assert state.account_id == second.principal_context.principal_context_id
    assert state.unexplained_balance_delta is not None
    assert str(state.unexplained_balance_delta.amount) == "-0.02"


def test_deleting_reconciliation_history_cannot_pristine_rebootstrap_continuity(
    monkeypatch,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    authority = tmp_path / "authority"
    store_path = workspace / "betdaq-account.json"

    evidence, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )
    store = BookmakerAccountReconciliationStore(
        store_path,
        authority_root=authority,
    )
    assert append_to_reconciliation(store, evidence) is True
    store_path.unlink()

    restarted = BookmakerAccountReconciliationStore(
        store_path,
        authority_root=authority,
    )
    with pytest.raises(AccountReconciliationIntegrityError):
        restarted.latest_state()


def test_legacy_session_scoped_history_requires_explicit_migration_proof(
    monkeypatch,
):
    current, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )
    legacy = current.source_evidence.snapshot

    assert legacy.profile.account_id.startswith("betdaq-auth-context:")
    assert legacy.profile.account_id != current.snapshot.profile.account_id
    with pytest.raises(
        BetdaqAccountContinuityMigrationRequiredError,
        match="explicit migration proof",
    ):
        require_reconciliation_history_compatible(legacy, current)


def test_compatible_continuous_history_passes_preflight(monkeypatch):
    current, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )

    require_reconciliation_history_compatible(current.snapshot, current)


def test_caller_reconstructed_continuity_object_cannot_authorize_reconciliation(
    monkeypatch,
    tmp_path,
):
    current, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )
    forged = BetdaqContinuousAccountEvidence(
        snapshot=current.snapshot,
        source_evidence=current.source_evidence,
        principal_context=current.principal_context,
    )
    store = BookmakerAccountReconciliationStore(
        tmp_path / "workspace" / "betdaq-account.json",
        authority_root=tmp_path / "authority",
    )

    with pytest.raises(
        BetdaqAccountContinuityError,
        match="product-issued BETDAQ continuity evidence",
    ):
        append_to_reconciliation(store, forged)

    assert store.latest_snapshot() is None


def test_rebound_issuance_predicate_cannot_authorize_forged_reconciliation(
    monkeypatch,
    tmp_path,
):
    current, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )
    forged = BetdaqContinuousAccountEvidence(
        snapshot=current.snapshot,
        source_evidence=current.source_evidence,
        principal_context=current.principal_context,
    )
    store = BookmakerAccountReconciliationStore(
        tmp_path / "workspace" / "betdaq-account.json",
        authority_root=tmp_path / "authority",
    )

    monkeypatch.setattr(
        continuity_module,
        "_is_product_issued_continuity_evidence",
        lambda _value: True,
        raising=False,
    )
    with pytest.raises(
        BetdaqAccountContinuityError,
        match="continuity issuance authority was exposed",
    ):
        append_to_reconciliation(store, forged)

    assert store.latest_snapshot() is None


def test_dataclass_replace_cannot_copy_product_issuance_authority(
    monkeypatch,
    tmp_path,
):
    current, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )
    copied = replace(current)
    assert copied is not current

    store = BookmakerAccountReconciliationStore(
        tmp_path / "workspace" / "betdaq-account.json",
        authority_root=tmp_path / "authority",
    )
    with pytest.raises(
        BetdaqAccountContinuityError,
        match="product-issued BETDAQ continuity evidence",
    ):
        append_to_reconciliation(store, copied)

    assert store.latest_snapshot() is None


def test_same_object_payload_mutation_revokes_product_issuance_authority(
    monkeypatch,
    tmp_path,
):
    current, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )
    assert current.snapshot.balance is not None
    forged_balance = replace(
        current.snapshot.balance,
        available_balance=Decimal("999.99"),
    )
    forged_snapshot = replace(current.snapshot, balance=forged_balance)

    # Frozen dataclasses remain mutable through object.__setattr__. The issued
    # outer object keeps the same id, so an identity-only issuer registry would
    # incorrectly preserve authority for this caller-modified payload.
    object.__setattr__(current, "snapshot", forged_snapshot)

    store = BookmakerAccountReconciliationStore(
        tmp_path / "workspace" / "betdaq-account.json",
        authority_root=tmp_path / "authority",
    )
    with pytest.raises(
        BetdaqAccountContinuityError,
        match="product-issued BETDAQ continuity evidence",
    ):
        append_to_reconciliation(store, current)

    assert store.latest_snapshot() is None


def test_reconciliation_store_subclass_cannot_bypass_canonical_durability(
    monkeypatch,
    tmp_path,
):
    current, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )

    class RedirectingStore(BookmakerAccountReconciliationStore):
        def latest_snapshot(self):
            raise AssertionError("virtual latest_snapshot must not be trusted")

        def append_snapshot(self, _snapshot):
            return True

    store = RedirectingStore(
        tmp_path / "workspace" / "betdaq-account.json",
        authority_root=tmp_path / "authority",
    )

    with pytest.raises(
        BetdaqAccountContinuityError,
        match="not the canonical durable #790 store",
    ):
        append_to_reconciliation(store, current)


def test_reconciliation_store_instance_method_shadow_cannot_report_success(
    monkeypatch,
    tmp_path,
):
    current, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )
    store = BookmakerAccountReconciliationStore(
        tmp_path / "workspace" / "betdaq-account.json",
        authority_root=tmp_path / "authority",
    )
    monkeypatch.setattr(store, "append_snapshot", lambda _snapshot: True)

    with pytest.raises(
        BetdaqAccountContinuityError,
        match="reconciliation store dispatch was shadowed",
    ):
        append_to_reconciliation(store, current)

    assert store.latest_snapshot() is None


def test_rebound_reconciliation_store_class_cannot_replace_790_authority(
    monkeypatch,
    tmp_path,
):
    current, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )
    store = BookmakerAccountReconciliationStore(
        tmp_path / "workspace" / "betdaq-account.json",
        authority_root=tmp_path / "authority",
    )

    class RedirectingStore:
        pass

    monkeypatch.setattr(
        continuity_module,
        "BookmakerAccountReconciliationStore",
        RedirectingStore,
    )

    with pytest.raises(
        BetdaqAccountContinuityError,
        match="reconciliation store class was replaced",
    ):
        append_to_reconciliation(store, current)

    assert store.latest_snapshot() is None

def test_reconciliation_monotonic_authority_replacement_cannot_report_success(
    monkeypatch,
    tmp_path,
):
    current, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )
    store_path = tmp_path / "workspace" / "betdaq-account.json"
    store = BookmakerAccountReconciliationStore(
        store_path,
        authority_root=tmp_path / "authority",
    )

    class NoOpAuthority:
        def read_history(self):
            return ()

        def recover(self, **_kwargs):
            return None

        def prepare(self, **_kwargs):
            return None

    store._authority = NoOpAuthority()

    with pytest.raises(
        BetdaqAccountContinuityError,
        match="reconciliation monotonic authority was replaced",
    ):
        append_to_reconciliation(store, current)

    assert not store_path.exists()


@pytest.mark.parametrize("method_name", ("read_history", "prepare", "recover"))
def test_reconciliation_monotonic_authority_method_shadow_cannot_report_success(
    monkeypatch,
    tmp_path,
    method_name,
):
    current, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )
    store_path = tmp_path / "workspace" / "betdaq-account.json"
    store = BookmakerAccountReconciliationStore(
        store_path,
        authority_root=tmp_path / "authority",
    )
    monkeypatch.setattr(store._authority, method_name, lambda *args, **kwargs: None)

    with pytest.raises(
        BetdaqAccountContinuityError,
        match="reconciliation monotonic authority dispatch changed",
    ):
        append_to_reconciliation(store, current)

    assert not store_path.exists()


def test_reconciliation_monotonic_authority_path_binding_cannot_drift(
    monkeypatch,
    tmp_path,
):
    current, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )
    original_path = tmp_path / "workspace" / "betdaq-account.json"
    store = BookmakerAccountReconciliationStore(
        original_path,
        authority_root=tmp_path / "authority",
    )
    store.path = original_path.with_name("caller-retargeted.json")

    with pytest.raises(
        BetdaqAccountContinuityError,
        match="reconciliation monotonic authority binding changed",
    ):
        append_to_reconciliation(store, current)

    assert not original_path.exists()
    assert not store.path.exists()


@pytest.mark.parametrize(
    "helper_name",
    (
        "_load_history",
        "_recover_authority",
        "_require_same_account",
        "_require_nested_evidence_after",
        "_reconcile",
        "_write_history",
        "_encode_history",
        "_next_authority_tx_id",
        "_publish_history_bytes",
    ),
)
def test_reconciliation_lower_helper_rebind_cannot_report_success(
    monkeypatch,
    tmp_path,
    helper_name,
):
    current, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )
    store_path = tmp_path / "workspace" / "betdaq-account.json"
    store = BookmakerAccountReconciliationStore(
        store_path,
        authority_root=tmp_path / "authority",
    )

    monkeypatch.setattr(
        BookmakerAccountReconciliationStore,
        helper_name,
        lambda *args, **kwargs: None,
    )

    with pytest.raises(
        BetdaqAccountContinuityError,
        match="reconciliation store helper dispatch changed",
    ):
        append_to_reconciliation(store, current)

    assert not store_path.exists()


@pytest.mark.parametrize(
    "helper_name",
    (
        "_load_history",
        "_recover_authority",
        "_require_same_account",
        "_require_nested_evidence_after",
        "_reconcile",
        "_write_history",
        "_encode_history",
        "_next_authority_tx_id",
        "_publish_history_bytes",
    ),
)
def test_reconciliation_lower_helper_instance_shadow_cannot_report_success(
    monkeypatch,
    tmp_path,
    helper_name,
):
    current, _ = acquire_balance(
        monkeypatch,
        BetdaqCredentials("alice", "password", "app"),
        clock=at(0, 1),
    )
    store_path = tmp_path / "workspace" / "betdaq-account.json"
    store = BookmakerAccountReconciliationStore(
        store_path,
        authority_root=tmp_path / "authority",
    )

    monkeypatch.setattr(
        store,
        helper_name,
        lambda *args, **kwargs: None,
    )

    with pytest.raises(
        BetdaqAccountContinuityError,
        match="reconciliation store helper dispatch was shadowed",
    ):
        append_to_reconciliation(store, current)

    assert not store_path.exists()


@pytest.mark.parametrize(
    "helper_name",
    (
        "_credential_material",
        "_require_canonical_source_authority",
        "_require_canonical_source_instance",
        "_principal_context",
        "_project_snapshot",
        "_issue_continuous_evidence",
        "_is_product_issued_continuity_evidence",
    ),
)
def test_positive_continuity_rejects_post_import_composition_helper_rebind(
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    opener = QueueUrlopen(balance())
    monkeypatch.setattr(betdaq_account_module, "urlopen", opener)
    client = BetdaqAccountContinuityClient(
        BetdaqCredentials("alice", "password", "application"),
        clock=at(0, 1),
    )
    attacker_calls: list[str] = []

    def attacker(*args, **kwargs):
        del args, kwargs
        attacker_calls.append(helper_name)
        raise AssertionError("attacker-controlled continuity helper executed")

    monkeypatch.setattr(
        continuity_module,
        helper_name,
        attacker,
        raising=False,
    )

    with pytest.raises(
        BetdaqAccountContinuityError,
        match="canonical BETDAQ continuity",
    ):
        client.read_account_evidence(
            frozenset({BookmakerCapability.BALANCE_READ})
        )

    assert attacker_calls == []
    assert opener.calls == []

