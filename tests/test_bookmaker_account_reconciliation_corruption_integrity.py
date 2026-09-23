from decimal import Decimal
import json

import pytest

from autosport.bookmaker_account_reconciliation import (
    AccountReconciliationIntegrityError,
    BookmakerAccountReconciliationStore,
    snapshot_fingerprint,
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
_SOURCE_SHA256 = "a" * 64


@pytest.fixture(autouse=True)
def _isolated_monotonic_authority_root(tmp_path, monkeypatch) -> None:
    authority_root = (
        tmp_path.parent / f".{tmp_path.name}-monotonic-authority"
    ).resolve(strict=False)
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(authority_root))


def _snapshot(amount: str) -> BookmakerAccountSnapshot:
    capability = BookmakerCapability.BALANCE_READ
    profile = BookmakerCapabilityProfile(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                capability=capability,
                state=BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=_OBSERVED_AT,
        source_ref="capability-probe",
        source_payload_sha256=_SOURCE_SHA256,
    )
    balance = BookmakerBalanceObservation(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        observation_id="balance-1",
        currency="EUR",
        available_balance=Decimal(amount),
        observed_at=_OBSERVED_AT,
        source_payload_sha256=_SOURCE_SHA256,
    )
    return BookmakerAccountSnapshot(
        profile=profile,
        observed_capabilities=frozenset({capability}),
        observed_at=_OBSERVED_AT,
        balance=balance,
    )


def test_recomputed_local_snapshot_digest_cannot_mint_money_authority(tmp_path) -> None:
    """A caller-recomputed local checksum must not authorize forged money evidence."""

    path = tmp_path / "account.json"
    original = _snapshot("100")
    store = BookmakerAccountReconciliationStore(path)
    assert store.append_snapshot(original) is True

    document = json.loads(path.read_text(encoding="utf-8"))
    forged = _snapshot("999")
    document["snapshots"][0]["snapshot"]["balance"]["available_balance"] = "999"

    # Make the tampered record locally self-consistent. This deliberately defeats the
    # store's inner snapshot_id check, so acceptance now depends on the independent
    # monotonic authority rather than on a caller-recomputable checksum.
    forged_snapshot_id = snapshot_fingerprint(forged)
    document["snapshots"][0]["snapshot_id"] = forged_snapshot_id
    assert document["snapshots"][0]["snapshot_id"] != snapshot_fingerprint(original)

    path.write_text(
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="independent monotonic authority validation",
    ):
        BookmakerAccountReconciliationStore(path).latest_state()
