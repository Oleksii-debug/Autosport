from decimal import Decimal, localcontext

from autosport.provider_quote_comparison import (
    ProviderQuoteComparison,
    ProviderQuotePoint,
)


_HASH_A = "a" * 64
_HASH_B = "b" * 64
_SNAPSHOT_HASH = "c" * 64
_TS = "2026-09-21T14:00:00Z"


def _quote(source_id: str, odds: str, event_hash: str) -> ProviderQuotePoint:
    return ProviderQuotePoint(
        source_id=source_id,
        provider_source_class="bookmaker",
        sequence=1,
        decimal_odds=Decimal(odds),
        observed_ts=_TS,
        source_ts=_TS,
        ingest_ts=_TS,
        market_event_sha256=event_hash,
    )


def _comparison() -> ProviderQuoteComparison:
    return ProviderQuoteComparison(
        mirror_revision=7,
        mirror_snapshot_sha256=_SNAPSHOT_HASH,
        sport="football",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        competition_id="competition-1",
        market_semantics_id="match-winner",
        as_of=_TS,
        max_age_microseconds=5_000_000,
        quotes=(
            _quote(
                "provider-a",
                "1.00000000000000000000000000000000001",
                _HASH_A,
            ),
            _quote(
                "provider-b",
                "2.12345678901234567890123456789012345",
                _HASH_B,
            ),
        ),
    )


def test_displayed_spread_and_hash_ignore_ambient_decimal_precision() -> None:
    comparison = _comparison()
    expected = Decimal("1.12345678901234567890123456789012344")

    with localcontext() as context:
        context.prec = 6
        low_precision_spread = comparison.displayed_spread
        low_precision_payload = comparison.to_payload()
        low_precision_hash = comparison.comparison_sha256

    with localcontext() as context:
        context.prec = 50
        high_precision_spread = comparison.displayed_spread
        high_precision_payload = comparison.to_payload()
        high_precision_hash = comparison.comparison_sha256

    assert low_precision_spread == expected
    assert high_precision_spread == expected
    assert low_precision_spread.as_tuple() == high_precision_spread.as_tuple()
    assert low_precision_payload == high_precision_payload
    assert low_precision_hash == high_precision_hash


def test_same_comparison_object_does_not_rehash_when_precision_changes() -> None:
    comparison = _comparison()

    hashes = []
    spreads = []
    for precision in (4, 9, 28, 80):
        with localcontext() as context:
            context.prec = precision
            spreads.append(str(comparison.displayed_spread))
            hashes.append(comparison.comparison_sha256)

    assert len(set(spreads)) == 1
    assert len(set(hashes)) == 1
