from __future__ import annotations

from decimal import Decimal

import pytest

import autosport.bookmaker_account_reconciliation as reconciliation_module
from autosport.bookmaker_account_reconciliation import (
    AccountReconciliationIntegrityError,
    BookmakerAccountReconciliationStore,
)
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
    return tmp_path.parent / f".{tmp_path.name}-{name}"


def _snapshot(amount: Decimal) -> BookmakerAccountSnapshot:
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
        observed_at=_OBSERVED_AT,
        source_ref="semantic-constructor-binding-regression",
        source_payload_sha256=_HASH,
    )
    balance = BookmakerBalanceObservation(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        observation_id="balance-1",
        currency="EUR",
        available_balance=amount,
        observed_at=_OBSERVED_AT,
        source_payload_sha256=_HASH,
    )
    return BookmakerAccountSnapshot(
        profile=profile,
        observed_capabilities=frozenset({BookmakerCapability.BALANCE_READ}),
        observed_at=_OBSERVED_AT,
        balance=balance,
    )


@pytest.mark.parametrize(
    "binding_name",
    ("Decimal", "BookmakerCapability", "BookmakerCapabilityState"),
)
def test_semantic_constructor_binding_rebind_fails_before_durable_decode(
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

    canonical_binding = getattr(reconciliation_module, binding_name)
    callback_reached = False

    def hostile_binding(*args, **kwargs):
        nonlocal callback_reached
        callback_reached = True
        return canonical_binding(*args, **kwargs)

    monkeypatch.setattr(
        reconciliation_module,
        binding_name,
        hostile_binding,
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="transitive module dispatch graph changed",
    ):
        store.latest_snapshot()

    assert callback_reached is False
