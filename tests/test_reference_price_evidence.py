from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.domain import MarketEvent, MarketType
from autosport.market_mirror import MirrorSnapshot
from autosport.reference_price_evidence import build_reference_price_evidence


AS_OF = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
MAX_AGE = timedelta(seconds=30)
MAX_SPREAD = timedelta(seconds=10)


def _event(
    source_id: str,
    odds: str,
    *,
    seconds_before: int,
    sequence: int = 1,
    status: str = "open",
    sport: str | None = "football",
    competition_id: str | None = "epl",
    market_semantics_id: str | None = "match-winner",
    observed_offset: int | None = None,
    ingest_offset: int | None = None,
) -> MarketEvent:
    effective = AS_OF - timedelta(seconds=seconds_before)
    observed = AS_OF - timedelta(
        seconds=seconds_before if observed_offset is None else observed_offset
    )
    ingested = AS_OF - timedelta(
        seconds=seconds_before if ingest_offset is None else ingest_offset
    )
    return MarketEvent(
        event_id="ars-che-20260921",
        market_id="match-winner",
        selection_id="arsenal",
        decimal_odds=Decimal(odds),
        observed_ts=observed.isoformat(),
        source_id=source_id,
        sequence=sequence,
        market_type=MarketType.WINNER,
        status=status,
        source_ts=effective.isoformat(),
        ingest_ts=ingested.isoformat(),
        sport=sport,
        competition_id=competition_id,
        market_semantics_id=market_semantics_id,
        provider_source_class="official-api",
    )


def _build(events: tuple[MarketEvent, ...], **kwargs):
    return build_reference_price_evidence(
        MirrorSnapshot(revision=17, events=events),
        as_of=AS_OF,
        max_age=MAX_AGE,
        max_source_spread=MAX_SPREAD,
        **kwargs,
    )


def test_reference_evidence_is_order_independent_and_preserves_even_median_band() -> None:
    events = (
        _event("provider-c", "2.30", seconds_before=6, sequence=3),
        _event("provider-a", "2.00", seconds_before=4, sequence=7),
        _event("provider-d", "2.40", seconds_before=8, sequence=4),
        _event("provider-b", "2.10", seconds_before=5, sequence=9),
    )

    forward = _build(events)
    reverse = _build(tuple(reversed(events)))

    assert forward == reverse
    assert forward.source_ids == (
        "provider-a",
        "provider-b",
        "provider-c",
        "provider-d",
    )
    assert forward.minimum_decimal_odds == Decimal("2.00")
    assert forward.lower_median_decimal_odds == Decimal("2.10")
    assert forward.upper_median_decimal_odds == Decimal("2.30")
    assert forward.maximum_decimal_odds == Decimal("2.40")
    assert forward.evidence_sha256 == reverse.evidence_sha256
    assert len(forward.evidence_sha256) == 64
    assert forward.action_authorized is False
    assert forward.fair_probability_proven is False
    assert forward.execution_price_proven is False


def test_odd_provider_count_has_one_observed_median_price() -> None:
    evidence = _build(
        (
            _event("provider-a", "1.90", seconds_before=4),
            _event("provider-b", "2.05", seconds_before=5),
            _event("provider-c", "2.20", seconds_before=6),
        )
    )

    assert evidence.lower_median_decimal_odds == Decimal("2.05")
    assert evidence.upper_median_decimal_odds == Decimal("2.05")


def test_reference_evidence_rejects_too_few_or_duplicate_sources() -> None:
    with pytest.raises(ValueError, match="at least min_sources"):
        _build((_event("provider-a", "2.00", seconds_before=4),))

    duplicate = (
        _event("provider-a", "2.00", seconds_before=4, sequence=1),
        _event("provider-a", "2.10", seconds_before=5, sequence=2),
    )
    with pytest.raises(ValueError, match="distinct provider source_ids"):
        _build(duplicate)


def test_reference_evidence_rejects_mixed_quote_or_market_semantics() -> None:
    base = _event("provider-a", "2.00", seconds_before=4)
    wrong_semantics = _event(
        "provider-b",
        "2.10",
        seconds_before=5,
        market_semantics_id="draw-no-bet",
    )
    with pytest.raises(ValueError, match="quote or market semantic identities"):
        _build((base, wrong_semantics))

    wrong_selection = _event("provider-b", "2.10", seconds_before=5)
    object.__setattr__(wrong_selection, "selection_id", "chelsea")
    with pytest.raises(ValueError, match="quote or market semantic identities"):
        _build((base, wrong_selection))


def test_reference_evidence_requires_explicit_sport_and_market_semantics() -> None:
    with pytest.raises(ValueError, match="explicit canonical sport"):
        _build(
            (
                _event("provider-a", "2.00", seconds_before=4, sport=None),
                _event("provider-b", "2.10", seconds_before=5, sport=None),
            )
        )

    with pytest.raises(ValueError, match="market_semantics_id"):
        _build(
            (
                _event(
                    "provider-a",
                    "2.00",
                    seconds_before=4,
                    market_semantics_id=None,
                ),
                _event(
                    "provider-b",
                    "2.10",
                    seconds_before=5,
                    market_semantics_id=None,
                ),
            )
        )


def test_reference_evidence_revalidates_freshness_and_source_spread() -> None:
    stale = (
        _event("provider-a", "2.00", seconds_before=4),
        _event("provider-b", "2.10", seconds_before=31),
    )
    with pytest.raises(ValueError, match="stale market evidence"):
        _build(stale)

    wide = (
        _event("provider-a", "2.00", seconds_before=1),
        _event("provider-b", "2.10", seconds_before=12),
    )
    with pytest.raises(ValueError, match="max_source_spread"):
        _build(wide)


def test_provider_timestamp_is_freshness_authority_when_present() -> None:
    fresh_local_but_stale_provider = _event(
        "provider-b",
        "2.10",
        seconds_before=31,
        observed_offset=1,
        ingest_offset=1,
    )
    with pytest.raises(ValueError, match="stale market evidence"):
        _build(
            (
                _event("provider-a", "2.00", seconds_before=2),
                fresh_local_but_stale_provider,
            )
        )


def test_hand_built_snapshot_cannot_inject_future_local_evidence() -> None:
    future_observed = _event(
        "provider-b",
        "2.10",
        seconds_before=3,
        observed_offset=-1,
    )
    with pytest.raises(ValueError, match="future market evidence"):
        _build(
            (
                _event("provider-a", "2.00", seconds_before=2),
                future_observed,
            )
        )

    future_ingest = _event(
        "provider-b",
        "2.10",
        seconds_before=3,
        ingest_offset=-1,
    )
    with pytest.raises(ValueError, match="future market evidence"):
        _build(
            (
                _event("provider-a", "2.00", seconds_before=2),
                future_ingest,
            )
        )


def test_reference_evidence_rejects_inactive_event_and_missing_required_source() -> None:
    with pytest.raises(ValueError, match="only open"):
        _build(
            (
                _event("provider-a", "2.00", seconds_before=2),
                _event(
                    "provider-b",
                    "2.10",
                    seconds_before=3,
                    status="suspended",
                ),
            )
        )

    with pytest.raises(ValueError, match="missing a required"):
        _build(
            (
                _event("provider-a", "2.00", seconds_before=2),
                _event("provider-b", "2.10", seconds_before=3),
            ),
            required_source_ids=("provider-a", "provider-c"),
        )


def test_truth_flags_are_not_constructor_inputs() -> None:
    evidence = _build(
        (
            _event("provider-a", "2.00", seconds_before=2),
            _event("provider-b", "2.10", seconds_before=3),
        )
    )

    init_by_name = {field.name: field.init for field in fields(type(evidence))}
    assert init_by_name["action_authorized"] is False
    assert init_by_name["fair_probability_proven"] is False
    assert init_by_name["execution_price_proven"] is False


def test_digest_binds_revision_and_exact_source_provenance() -> None:
    events = (
        _event("provider-a", "2.00", seconds_before=2, sequence=4),
        _event("provider-b", "2.10", seconds_before=3, sequence=8),
    )
    first = _build(events)
    second = build_reference_price_evidence(
        MirrorSnapshot(revision=18, events=events),
        as_of=AS_OF,
        max_age=MAX_AGE,
        max_source_spread=MAX_SPREAD,
    )

    assert first.source_sequences == (("provider-a", 4), ("provider-b", 8))
    assert first.evidence_sha256 != second.evidence_sha256
