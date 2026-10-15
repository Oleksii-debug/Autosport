from __future__ import annotations

from decimal import Decimal
from hashlib import sha256

import pytest

from autosport.betfair_account_readonly import BetfairEvidence
from autosport.betfair_provider_billing_inputs import (
    BetfairAccountStatementItemObservation,
    BetfairAccountStatementPageObservation,
    BetfairDeveloperAppEntitlementObservation,
    BetfairProviderBillingInputsObservation,
)
from autosport.betfair_statement_completeness import (
    BetfairStatementCompletenessError,
    _derive_from_verified_pages,
)


def _hex(label: str) -> str:
    return sha256(label.encode("utf-8")).hexdigest()


def _row(label: str, item_date: str) -> BetfairAccountStatementItemObservation:
    return BetfairAccountStatementItemObservation(
        ref_id=f"ref-{label}",
        item_date=item_date,
        amount=Decimal("1"),
        balance=Decimal("100"),
        item_class="UNKNOWN",
        item_class_data_sha256=_hex(f"row:{label}"),
    )


def _entitlement() -> BetfairDeveloperAppEntitlementObservation:
    return BetfairDeveloperAppEntitlementObservation(
        venue_id="betfair",
        app_id=41,
        app_name="autosport",
        version_id=7,
        version="1.0",
        delay_data=False,
        subscription_required=False,
        owner_managed=False,
        active=True,
        vendor_id="vendor-3",
        provider_owner="owner-a",
        observed_at="2026-09-21T00:00:00+00:00",
        source_projection_sha256=_hex("projection"),
    )


def _page(
    offset: int,
    rows: tuple[BetfairAccountStatementItemObservation, ...],
    *,
    more_available: bool,
    observed_at: str,
    source_label: str,
) -> BetfairProviderBillingInputsObservation:
    statement = object.__new__(BetfairAccountStatementPageObservation)
    object.__setattr__(statement, "venue_id", "betfair")
    object.__setattr__(statement, "currency_code", "GBP")
    object.__setattr__(statement, "from_record", offset)
    object.__setattr__(statement, "record_count", 2)
    object.__setattr__(
        statement,
        "statement_from",
        "2026-09-01T00:00:00+00:00",
    )
    object.__setattr__(
        statement,
        "statement_to",
        "2026-09-21T23:59:59+00:00",
    )
    object.__setattr__(statement, "request_scope_sha256", _hex(f"scope:{offset}"))
    object.__setattr__(statement, "items", rows)
    object.__setattr__(statement, "more_available", more_available)
    object.__setattr__(
        statement,
        "account_details_evidence",
        BetfairEvidence(observed_at, _hex(f"details:{source_label}")),
    )
    object.__setattr__(
        statement,
        "statement_evidence",
        BetfairEvidence(observed_at, _hex(f"statement:{source_label}")),
    )

    source = object.__new__(BetfairProviderBillingInputsObservation)
    object.__setattr__(source, "entitlement", _entitlement())
    object.__setattr__(source, "statement", statement)
    object.__setattr__(source, "observed_at", observed_at)
    object.__setattr__(source, "evidence_sha256", _hex(f"source:{source_label}"))
    return source


def test_terminal_offset_sweep_rejects_non_chronological_statement_rows() -> None:
    # Betfair documents AccountStatementReport as chronologically ordered, but the
    # public text does not need to be interpreted as ascending vs descending here:
    # t1,t3,t2 is not monotone in either direction.
    pages = (
        _page(
            0,
            (
                _row("a", "2026-09-21T00:00:01+00:00"),
                _row("b", "2026-09-21T00:00:03+00:00"),
            ),
            more_available=True,
            observed_at="2026-09-21T00:00:04+00:00",
            source_label="page-a",
        ),
        _page(
            2,
            (_row("c", "2026-09-21T00:00:02+00:00"),),
            more_available=False,
            observed_at="2026-09-21T00:00:05+00:00",
            source_label="page-b",
        ),
    )

    with pytest.raises(BetfairStatementCompletenessError, match="chronolog"):
        _derive_from_verified_pages(pages)
