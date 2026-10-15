from __future__ import annotations

import copy
from dataclasses import fields
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
    BetfairStatementPaginationEvidence,
    _derive_from_verified_pages,
    resolve_betfair_statement_pagination_completeness,
    validate_betfair_statement_pagination_evidence,
)


def _hex(label: str) -> str:
    return sha256(label.encode("utf-8")).hexdigest()


def _row(
    label: str,
    *,
    ref_id: str | None = None,
    item_date: str = "2026-09-21T00:00:00+00:00",
) -> BetfairAccountStatementItemObservation:
    return BetfairAccountStatementItemObservation(
        ref_id=ref_id or f"ref-{label}",
        item_date=item_date,
        amount=Decimal("1.25"),
        balance=Decimal("100.00"),
        item_class="UNKNOWN",
        item_class_data_sha256=_hex(f"row-data:{label}"),
    )


def _entitlement(
    *,
    provider_owner: str = "owner-a",
    projection_label: str = "projection-a",
) -> BetfairDeveloperAppEntitlementObservation:
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
        provider_owner=provider_owner,
        observed_at="2026-09-21T00:00:00+00:00",
        source_projection_sha256=_hex(projection_label),
    )


def _page(
    offset: int,
    rows: tuple[BetfairAccountStatementItemObservation, ...],
    *,
    more_available: bool,
    observed_at: str,
    record_count: int = 2,
    statement_from: str | None = "2026-09-01T00:00:00+00:00",
    statement_to: str | None = "2026-09-21T23:59:59+00:00",
    currency_code: str = "GBP",
    entitlement: BetfairDeveloperAppEntitlementObservation | None = None,
    source_label: str | None = None,
) -> BetfairProviderBillingInputsObservation:
    # These exact-type wrappers deliberately bypass upstream construction only for
    # structural tests. The production resolver rejects them because they were not
    # issued by the canonical provider-billing acquisition authority.
    statement = object.__new__(BetfairAccountStatementPageObservation)
    object.__setattr__(statement, "venue_id", "betfair")
    object.__setattr__(statement, "currency_code", currency_code)
    object.__setattr__(statement, "from_record", offset)
    object.__setattr__(statement, "record_count", record_count)
    object.__setattr__(statement, "statement_from", statement_from)
    object.__setattr__(statement, "statement_to", statement_to)
    object.__setattr__(statement, "request_scope_sha256", _hex(f"scope:{offset}"))
    object.__setattr__(statement, "items", rows)
    object.__setattr__(statement, "more_available", more_available)
    object.__setattr__(
        statement,
        "account_details_evidence",
        BetfairEvidence(observed_at, _hex(f"details:{offset}:{source_label}")),
    )
    object.__setattr__(
        statement,
        "statement_evidence",
        BetfairEvidence(observed_at, _hex(f"statement:{offset}:{source_label}")),
    )

    source = object.__new__(BetfairProviderBillingInputsObservation)
    object.__setattr__(source, "entitlement", entitlement or _entitlement())
    object.__setattr__(source, "statement", statement)
    object.__setattr__(source, "observed_at", observed_at)
    object.__setattr__(
        source,
        "evidence_sha256",
        _hex(source_label or f"source:{offset}:{observed_at}"),
    )
    return source


def _complete_two_page_scan() -> tuple[BetfairProviderBillingInputsObservation, ...]:
    return (
        _page(
            0,
            (_row("a"), _row("b")),
            more_available=True,
            observed_at="2026-09-21T00:00:01+00:00",
            source_label="page-a",
        ),
        _page(
            2,
            (_row("c"),),
            more_available=False,
            observed_at="2026-09-21T00:00:02+00:00",
            source_label="page-b",
        ),
    )


def test_structural_derivation_proves_only_terminal_pagination_traversal() -> None:
    result = _derive_from_verified_pages(_complete_two_page_scan())

    assert result.pagination_complete is True
    assert result.provider_origin_verified is True
    assert result.page_offsets == (0, 2)
    assert result.page_count == 2
    assert result.row_count == 3
    assert result.first_observed_at == "2026-09-21T00:00:01+00:00"
    assert result.last_observed_at == "2026-09-21T00:00:02+00:00"
    assert len(result.query_fingerprint_sha256) == 64
    assert len(result.ordered_rows_sha256) == 64
    assert len(result.evidence_sha256) == 64

    assert result.same_authenticated_session_proven is True
    assert result.acquisition_owned_traversal_proven is True
    assert result.coherent_snapshot_proven is False
    assert result.stable_account_identity_proven is False
    assert result.temporal_finality_attested is False
    assert result.temporal_finality_evidence_sha256 is None
    assert result.cost_scope_complete is False


def test_provider_ref_id_reuse_is_preserved_not_rejected_or_deduplicated() -> None:
    pages = (
        _page(
            0,
            (_row("a", ref_id="provider-reused-ref"),),
            more_available=True,
            observed_at="2026-09-21T00:00:01+00:00",
            source_label="reuse-a",
        ),
        _page(
            1,
            (
                _row(
                    "b",
                    ref_id="provider-reused-ref",
                    item_date="2026-09-21T00:00:03+00:00",
                ),
            ),
            more_available=False,
            observed_at="2026-09-21T00:00:02+00:00",
            source_label="reuse-b",
        ),
    )
    result = _derive_from_verified_pages(pages)
    assert result.row_count == 2
    assert result.page_offsets == (0, 1)


@pytest.mark.parametrize("bad_offset", [1, 3])
def test_first_or_later_noncontiguous_offset_fails_closed(bad_offset: int) -> None:
    pages = list(_complete_two_page_scan())
    if bad_offset == 1:
        pages[0] = _page(
            1,
            (_row("a"), _row("b")),
            more_available=True,
            observed_at="2026-09-21T00:00:01+00:00",
            source_label="bad-first",
        )
        match = "from_record=0"
    else:
        pages[1] = _page(
            3,
            (_row("c"),),
            more_available=False,
            observed_at="2026-09-21T00:00:02+00:00",
            source_label="gap",
        )
        match = "not contiguous"

    with pytest.raises(BetfairStatementCompletenessError, match=match):
        _derive_from_verified_pages(tuple(pages))


def test_overlap_offset_fails_closed() -> None:
    pages = (
        _page(
            0,
            (_row("a"), _row("b")),
            more_available=True,
            observed_at="2026-09-21T00:00:01+00:00",
            source_label="overlap-a",
        ),
        _page(
            1,
            (_row("c"),),
            more_available=False,
            observed_at="2026-09-21T00:00:02+00:00",
            source_label="overlap-b",
        ),
    )
    with pytest.raises(BetfairStatementCompletenessError, match="not contiguous"):
        _derive_from_verified_pages(pages)


def test_more_available_without_progress_fails_closed() -> None:
    pages = (
        _page(
            0,
            (),
            more_available=True,
            observed_at="2026-09-21T00:00:01+00:00",
            source_label="no-progress",
        ),
        _page(
            0,
            (_row("later"),),
            more_available=False,
            observed_at="2026-09-21T00:00:02+00:00",
            source_label="later",
        ),
    )
    with pytest.raises(BetfairStatementCompletenessError, match="no pagination progress"):
        _derive_from_verified_pages(pages)


def test_missing_terminal_page_fails_closed() -> None:
    page = _page(
        0,
        (_row("a"),),
        more_available=True,
        observed_at="2026-09-21T00:00:01+00:00",
        source_label="partial",
    )
    with pytest.raises(BetfairStatementCompletenessError, match="terminal page"):
        _derive_from_verified_pages((page,))


def test_pages_after_terminal_page_fail_closed() -> None:
    pages = (
        _page(
            0,
            (_row("a"),),
            more_available=False,
            observed_at="2026-09-21T00:00:01+00:00",
            source_label="terminal",
        ),
        _page(
            1,
            (_row("b"),),
            more_available=False,
            observed_at="2026-09-21T00:00:02+00:00",
            source_label="after-terminal",
        ),
    )
    with pytest.raises(BetfairStatementCompletenessError, match="after a provider terminal"):
        _derive_from_verified_pages(pages)


@pytest.mark.parametrize(
    "replacement",
    [
        {"record_count": 1},
        {"statement_from": "2026-09-02T00:00:00+00:00"},
        {"statement_to": "2026-09-20T23:59:59+00:00"},
        {"currency_code": "EUR"},
    ],
)
def test_query_scope_drift_across_pages_fails_closed(
    replacement: dict[str, object],
) -> None:
    first, _second = _complete_two_page_scan()
    kwargs: dict[str, object] = {
        "record_count": 2,
        "statement_from": "2026-09-01T00:00:00+00:00",
        "statement_to": "2026-09-21T23:59:59+00:00",
        "currency_code": "GBP",
    }
    kwargs.update(replacement)
    changed = _page(
        2,
        (_row("c"),),
        more_available=False,
        observed_at="2026-09-21T00:00:02+00:00",
        source_label="scope-drift",
        **kwargs,
    )
    with pytest.raises(BetfairStatementCompletenessError, match="scope changed"):
        _derive_from_verified_pages((first, changed))


def test_entitlement_scope_drift_across_pages_fails_closed() -> None:
    first, _second = _complete_two_page_scan()
    changed = _page(
        2,
        (_row("c"),),
        more_available=False,
        observed_at="2026-09-21T00:00:02+00:00",
        entitlement=_entitlement(
            provider_owner="owner-b",
            projection_label="projection-b",
        ),
        source_label="entitlement-drift",
    )
    with pytest.raises(BetfairStatementCompletenessError, match="scope changed"):
        _derive_from_verified_pages((first, changed))


def test_observation_time_regression_fails_closed() -> None:
    pages = (
        _page(
            0,
            (_row("a"),),
            more_available=True,
            observed_at="2026-09-21T00:00:02+00:00",
            source_label="time-a",
        ),
        _page(
            1,
            (_row("b"),),
            more_available=False,
            observed_at="2026-09-21T00:00:01+00:00",
            source_label="time-b",
        ),
    )
    with pytest.raises(BetfairStatementCompletenessError, match="moved backwards"):
        _derive_from_verified_pages(pages)


def test_public_resolver_rejects_structural_but_unissued_provider_page() -> None:
    forged = _complete_two_page_scan()[0]
    with pytest.raises(
        BetfairStatementCompletenessError,
        match="one canonical authenticated-session traversal",
    ):
        resolve_betfair_statement_pagination_completeness((forged,))


def test_structural_evidence_is_not_product_issuance() -> None:
    evidence = _derive_from_verified_pages(_complete_two_page_scan())
    with pytest.raises(
        BetfairStatementCompletenessError,
        match="must be product-issued",
    ):
        validate_betfair_statement_pagination_evidence(evidence)

    copied = copy.copy(evidence)
    assert copied == evidence
    assert copied is not evidence
    with pytest.raises(
        BetfairStatementCompletenessError,
        match="must be product-issued",
    ):
        validate_betfair_statement_pagination_evidence(copied)


def test_evidence_schema_cannot_mint_finality_or_cost_authority() -> None:
    names = {field.name for field in fields(BetfairStatementPaginationEvidence)}
    assert {
        "pagination_complete",
        "provider_origin_verified",
        "same_authenticated_session_proven",
        "acquisition_owned_traversal_proven",
        "coherent_snapshot_proven",
        "stable_account_identity_proven",
        "temporal_finality_attested",
        "temporal_finality_evidence_sha256",
        "cost_scope_complete",
    } <= names
    assert names.isdisjoint(
        {
            "known_zero",
            "known_amount",
            "allocated_amount",
            "allocation_fraction",
            "profit",
            "net_pnl",
            "settlement_authority",
            "execution_authority",
            "application_key",
            "session_token",
            "session_binding",
            "authenticated_session_id",
        }
    )


def test_constructor_cannot_claim_unavailable_finality_or_cost_completeness() -> None:
    base = _derive_from_verified_pages(_complete_two_page_scan())
    values = {field.name: getattr(base, field.name) for field in fields(base)}
    values["temporal_finality_attested"] = True
    values["cost_scope_complete"] = True

    with pytest.raises(
        BetfairStatementCompletenessError,
        match="temporal finality",
    ):
        BetfairStatementPaginationEvidence(**values)


def test_ordered_row_digest_changes_when_provider_view_changes() -> None:
    initial = _derive_from_verified_pages(_complete_two_page_scan())
    changed_pages = (
        _page(
            0,
            (_row("a"), _row("b")),
            more_available=True,
            observed_at="2026-09-21T00:00:03+00:00",
            source_label="changed-a",
        ),
        _page(
            2,
            (_row("correction", ref_id="ref-c"),),
            more_available=False,
            observed_at="2026-09-21T00:00:04+00:00",
            source_label="changed-b",
        ),
    )
    changed = _derive_from_verified_pages(changed_pages)

    assert changed.ordered_rows_sha256 != initial.ordered_rows_sha256
    assert changed.evidence_sha256 != initial.evidence_sha256
