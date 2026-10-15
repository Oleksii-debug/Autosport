from decimal import Decimal

import pytest

from autosport.matchbook_position_evidence import (
    MatchbookPositionEvidenceError,
    MatchbookPositionPage,
    MatchbookPositionPaginationError,
    MatchbookPositionScope,
    MatchbookPositionTraversal,
    MatchbookRunnerPositionObservation,
)

H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
TS1 = "2026-09-22T12:00:00Z"
TS2 = "2026-09-22T12:00:01Z"


def scope(**kwargs):
    values = dict(account_context_id="acct-ref", session_generation_id="session-gen")
    values.update(kwargs)
    return MatchbookPositionScope(**values)


def row(event=10, market=20, runner=30, profit="12.50", loss="-7.25", sha=H1):
    return MatchbookRunnerPositionObservation(
        provider_event_id=event,
        provider_market_id=market,
        provider_runner_id=runner,
        potential_profit=Decimal(profit),
        potential_loss=Decimal(loss),
        provider_row_sha256=sha,
    )


def page(*, sc=None, offset=0, per_page=2, total=1, rows=None, ts=TS1, req=H2, raw=H3):
    return MatchbookPositionPage(
        scope=sc or scope(),
        offset=offset,
        per_page=per_page,
        provider_total=total,
        observed_at_utc=ts,
        request_semantics_sha256=req,
        raw_response_sha256=raw,
        rows=tuple(rows if rows is not None else (row(),)),
    )


def test_scope_is_deterministic_and_order_independent():
    a = scope(event_ids=(2, 1), market_ids=(4, 3), runner_ids=(6, 5))
    b = scope(event_ids=(1, 2), market_ids=(3, 4), runner_ids=(5, 6))
    assert a.scope_id == b.scope_id
    assert a.event_ids == (1, 2)


@pytest.mark.parametrize("field", ["event_ids", "market_ids", "runner_ids"])
def test_scope_rejects_duplicate_ids(field):
    with pytest.raises(MatchbookPositionEvidenceError, match="duplicates"):
        scope(**{field: (1, 1)})


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "1"])
def test_scope_rejects_non_positive_or_wrong_typed_ids(bad):
    with pytest.raises(MatchbookPositionEvidenceError):
        scope(event_ids=(bad,))


def test_row_preserves_exact_signed_decimal_evidence():
    observed = row(profit="-0.00", loss="123456789.000000000000000001")
    assert observed.potential_profit == Decimal("-0.00")
    assert observed.potential_loss == Decimal("123456789.000000000000000001")
    assert "123456789000000000000000001e-18" in str(observed.to_payload())


@pytest.mark.parametrize("bad", [1.0, "1.0", Decimal("NaN"), Decimal("Infinity")])
def test_row_rejects_non_exact_or_non_finite_decimal(bad):
    with pytest.raises(MatchbookPositionEvidenceError, match="exact finite Decimal"):
        MatchbookRunnerPositionObservation(10, 20, 30, bad, Decimal("1"), H1)


def test_row_accepts_provider_fixture_one_sided_potential_loss():
    observed = MatchbookRunnerPositionObservation(
        414912311450010,
        414912312520010,
        414912312560010,
        None,
        Decimal("-5"),
        H1,
    )
    assert observed.potential_profit is None
    assert observed.potential_loss == Decimal("-5")


def test_row_accepts_provider_fixture_one_sided_potential_profit():
    observed = MatchbookRunnerPositionObservation(
        414912311450010,
        414912312520010,
        414912312600010,
        Decimal("6.45"),
        None,
        H1,
    )
    assert observed.potential_profit == Decimal("6.45")
    assert observed.potential_loss is None


def test_row_rejects_missing_both_potential_fields():
    with pytest.raises(MatchbookPositionEvidenceError, match="potential_profit or potential_loss"):
        MatchbookRunnerPositionObservation(10, 20, 30, None, None, H1)


def test_position_evidence_cannot_mint_canonical_economics_or_execution():
    observed = row()
    assert observed.stake_truth_proven is False
    assert observed.liability_truth_proven is False
    assert observed.gross_return_truth_proven is False
    assert observed.settlement_truth_proven is False
    assert observed.economic_pnl_truth_proven is False
    assert observed.execution_authority is False


def test_row_identity_includes_event_market_runner_and_exact_values():
    a = row()
    b = row(runner=31)
    c = row(profit="12.51")
    assert len({a.observation_id, b.observation_id, c.observation_id}) == 3


def test_page_accepts_row_inside_filters():
    sc = scope(event_ids=(10,), market_ids=(20,), runner_ids=(30,))
    observed = page(sc=sc)
    assert observed.page_reaches_reported_total is True
    assert observed.authenticated_provider_origin_proven is False
    assert observed.product_issued_request_semantics_proven is False


@pytest.mark.parametrize(
    "sc,message",
    [
        (scope(event_ids=(11,)), "event"),
        (scope(market_ids=(21,)), "market"),
        (scope(runner_ids=(31,)), "runner"),
    ],
)
def test_page_rejects_rows_outside_requested_filters(sc, message):
    with pytest.raises(MatchbookPositionEvidenceError, match=message):
        page(sc=sc)


def test_page_rejects_duplicate_runner_identity_even_with_different_wrapper_hash():
    with pytest.raises(MatchbookPositionEvidenceError, match="duplicated"):
        page(total=2, rows=(row(sha=H1), row(sha=H2)))


def test_page_rejects_rows_beyond_provider_total():
    with pytest.raises(MatchbookPositionEvidenceError, match="provider_total"):
        page(total=0, rows=(row(),))


def test_page_rejects_offset_beyond_provider_total():
    with pytest.raises(MatchbookPositionEvidenceError, match="offset"):
        page(offset=2, total=1, rows=())


def test_empty_zero_total_page_is_valid_structural_evidence():
    observed = page(total=0, rows=())
    traversal = MatchbookPositionTraversal(observed.scope, (observed,))
    assert traversal.structurally_complete is True
    assert traversal.authoritative_absence_proven is False


def test_complete_multi_page_traversal():
    sc = scope()
    p1 = page(sc=sc, offset=0, per_page=2, total=3, rows=(row(runner=30), row(runner=31, sha=H2)))
    p2 = page(sc=sc, offset=2, per_page=2, total=3, rows=(row(runner=32, sha=H3),), ts=TS2)
    traversal = MatchbookPositionTraversal(sc, (p1, p2))
    assert traversal.structurally_complete is True
    assert traversal.provider_snapshot_atomicity_proven is False
    assert traversal.authoritative_absence_proven is False
    assert traversal.canonical_position_truth_proven is False
    assert traversal.settlement_truth_proven is False
    assert traversal.economic_pnl_truth_proven is False
    assert traversal.execution_authority is False


def test_traversal_rejects_nonzero_start_offset():
    sc = scope()
    p = page(sc=sc, offset=1, total=1, rows=())
    with pytest.raises(MatchbookPositionPaginationError, match="offset 0"):
        MatchbookPositionTraversal(sc, (p,))


def test_traversal_rejects_gap():
    sc = scope()
    p1 = page(sc=sc, offset=0, per_page=1, total=3, rows=(row(runner=30),))
    p2 = page(sc=sc, offset=2, per_page=1, total=3, rows=(row(runner=31, sha=H2),), ts=TS2)
    with pytest.raises(MatchbookPositionPaginationError, match="contiguous"):
        MatchbookPositionTraversal(sc, (p1, p2))


def test_traversal_rejects_provider_total_drift():
    sc = scope()
    p1 = page(sc=sc, offset=0, per_page=1, total=2, rows=(row(runner=30),))
    p2 = page(sc=sc, offset=1, per_page=1, total=3, rows=(row(runner=31, sha=H2),), ts=TS2)
    with pytest.raises(MatchbookPositionPaginationError, match="provider_total changed"):
        MatchbookPositionTraversal(sc, (p1, p2))


def test_traversal_rejects_per_page_drift():
    sc = scope()
    p1 = page(sc=sc, offset=0, per_page=1, total=2, rows=(row(runner=30),))
    p2 = page(sc=sc, offset=1, per_page=2, total=2, rows=(row(runner=31, sha=H2),), ts=TS2)
    with pytest.raises(MatchbookPositionPaginationError, match="per_page changed"):
        MatchbookPositionTraversal(sc, (p1, p2))


def test_traversal_rejects_short_non_final_page():
    sc = scope()
    p1 = page(sc=sc, offset=0, per_page=2, total=3, rows=(row(runner=30),))
    p2 = page(sc=sc, offset=2, per_page=2, total=3, rows=(row(runner=31, sha=H2),), ts=TS2)
    with pytest.raises(MatchbookPositionPaginationError, match="non-final"):
        MatchbookPositionTraversal(sc, (p1, p2))


def test_traversal_rejects_missing_final_records():
    sc = scope()
    p1 = page(sc=sc, offset=0, per_page=2, total=3, rows=(row(runner=30), row(runner=31, sha=H2)))
    with pytest.raises(MatchbookPositionPaginationError, match="does not exhaust"):
        MatchbookPositionTraversal(sc, (p1,))


def test_traversal_rejects_time_rollback():
    sc = scope()
    p1 = page(sc=sc, offset=0, per_page=1, total=2, rows=(row(runner=30),), ts=TS2)
    p2 = page(sc=sc, offset=1, per_page=1, total=2, rows=(row(runner=31, sha=H2),), ts=TS1)
    with pytest.raises(MatchbookPositionPaginationError, match="rolled backward"):
        MatchbookPositionTraversal(sc, (p1, p2))


def test_traversal_rejects_duplicate_identity_across_pages():
    sc = scope()
    p1 = page(sc=sc, offset=0, per_page=1, total=2, rows=(row(sha=H1),))
    p2 = page(sc=sc, offset=1, per_page=1, total=2, rows=(row(sha=H2),), ts=TS2)
    with pytest.raises(MatchbookPositionPaginationError, match="repeats across pages"):
        MatchbookPositionTraversal(sc, (p1, p2))


def test_traversal_rejects_scope_substitution():
    sc1 = scope(event_ids=(10,))
    sc2 = scope(event_ids=(11,))
    p = page(sc=sc1)
    with pytest.raises(MatchbookPositionPaginationError, match="scope"):
        MatchbookPositionTraversal(sc2, (p,))


def test_require_observed_runner_rebinds_exact_native_identity_and_digest():
    sc = scope()
    p = page(sc=sc)
    traversal = MatchbookPositionTraversal(sc, (p,))
    assert traversal.require_observed_runner(
        provider_event_id=10,
        provider_market_id=20,
        provider_runner_id=30,
        provider_row_sha256=H1,
    ) == row()


def test_require_observed_runner_rejects_digest_substitution():
    sc = scope()
    traversal = MatchbookPositionTraversal(sc, (page(sc=sc),))
    with pytest.raises(MatchbookPositionEvidenceError, match="digest"):
        traversal.require_observed_runner(
            provider_event_id=10,
            provider_market_id=20,
            provider_runner_id=30,
            provider_row_sha256=H2,
        )


def test_require_observed_runner_rejects_absent_identity():
    sc = scope()
    traversal = MatchbookPositionTraversal(sc, (page(sc=sc),))
    with pytest.raises(MatchbookPositionEvidenceError, match="absent"):
        traversal.require_observed_runner(
            provider_event_id=10,
            provider_market_id=20,
            provider_runner_id=999,
            provider_row_sha256=H1,
        )


def test_digest_changes_when_potential_pnl_changes():
    sc = scope()
    a = page(sc=sc, rows=(row(profit="1", loss="-1"),))
    b = page(sc=sc, rows=(row(profit="2", loss="-1"),))
    assert a.page_evidence_id != b.page_evidence_id


def test_traversal_digest_is_stable_for_same_evidence():
    sc1 = scope(event_ids=(10, 9))
    sc2 = scope(event_ids=(9, 10))
    a = MatchbookPositionTraversal(sc1, (page(sc=sc1),))
    b = MatchbookPositionTraversal(sc2, (page(sc=sc2),))
    assert a.traversal_evidence_id == b.traversal_evidence_id


def test_provider_fixture_one_sided_rows_form_complete_structural_page():
    sc = scope()
    rows = (
        MatchbookRunnerPositionObservation(
            provider_event_id=414912311450010,
            provider_market_id=414912312520010,
            provider_runner_id=414912312560010,
            potential_profit=None,
            potential_loss=Decimal("-5"),
            provider_row_sha256=H1,
        ),
        MatchbookRunnerPositionObservation(
            provider_event_id=414912311450010,
            provider_market_id=414912312520010,
            provider_runner_id=414912312600010,
            potential_profit=Decimal("6.45"),
            potential_loss=None,
            provider_row_sha256=H2,
        ),
        MatchbookRunnerPositionObservation(
            provider_event_id=414912311450010,
            provider_market_id=414912312520010,
            provider_runner_id=414912312650010,
            potential_profit=None,
            potential_loss=Decimal("-5"),
            provider_row_sha256=H3,
        ),
    )
    observed = page(sc=sc, per_page=200, total=3, rows=rows)
    traversal = MatchbookPositionTraversal(sc, (observed,))

    assert traversal.structurally_complete is True
    assert observed.page_reaches_reported_total is True
    assert traversal.provider_snapshot_atomicity_proven is False
    assert traversal.authoritative_absence_proven is False
    assert traversal.economic_pnl_truth_proven is False


def test_zero_decimal_formatting_does_not_split_numeric_identity():
    variants = (Decimal("0"), Decimal("0.0"), Decimal("0.00"), Decimal("-0"), Decimal("-0.00"))
    observations = [
        MatchbookRunnerPositionObservation(10, 20, 30, value, None, H1)
        for value in variants
    ]
    assert {item.to_payload()["potential_profit"] for item in observations} == {"0e0"}
    assert len({item.observation_id for item in observations}) == 1


def test_trailing_zero_formatting_is_numeric_identity_not_fake_drift():
    a = MatchbookRunnerPositionObservation(10, 20, 30, Decimal("6.45"), None, H1)
    b = MatchbookRunnerPositionObservation(10, 20, 30, Decimal("6.4500"), None, H1)
    assert a.to_payload()["potential_profit"] == b.to_payload()["potential_profit"] == "645e-2"
    assert a.observation_id == b.observation_id
