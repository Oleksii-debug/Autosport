from decimal import Decimal

import pytest

from autosport.bookmaker_account_reconciliation import (
    AccountReconciliationIntegrityError,
    BookmakerAccountReconciliationStore,
    snapshot_fingerprint,
    snapshot_to_canonical_dict,
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
        source_ref="bounded-decimal-regression",
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
