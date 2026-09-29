from __future__ import annotations

import tempfile
from pathlib import Path

from autosport.providers import CanonicalNormalizer
from autosport.storage import SQLiteMarketStore
from autosport.the_odds_api_provider import HttpJsonResponse, TheOddsApiProvider


def _clock() -> str:
    return "2026-09-23T12:00:00Z"


def _historical_transport(
    *,
    snapshot_at: str,
    next_snapshot_at: str,
):
    payload = {
        "timestamp": snapshot_at,
        "previous_timestamp": None,
        "next_timestamp": next_snapshot_at,
        "data": [],
    }

    def transport(url: str, timeout: float) -> HttpJsonResponse:
        assert timeout > 0
        return HttpJsonResponse(
            payload=payload,
            status_code=200,
            headers={},
            final_url=url,
            body_sha256=None,
        )

    return transport


def test_default_current_scope_preserves_deployed_stream_identity() -> None:
    provider = TheOddsApiProvider("secret-a", sport="soccer_epl")

    assert provider.source_id == "the-odds-api:soccer_epl"


def test_same_sport_distinct_request_scopes_do_not_alias() -> None:
    european = TheOddsApiProvider(
        "secret-a",
        sport="soccer_epl",
        regions=("eu",),
        markets=("h2h", "totals"),
    )
    american = TheOddsApiProvider(
        "secret-a",
        sport="soccer_epl",
        regions=("us",),
        markets=("h2h", "totals"),
    )
    limited = TheOddsApiProvider(
        "secret-a",
        sport="soccer_epl",
        regions=("eu",),
        markets=("h2h", "totals"),
        include_bet_limits=True,
    )

    assert european.source_id != american.source_id
    assert european.source_id != limited.source_id
    assert american.source_id != limited.source_id


def test_equivalent_scope_order_and_api_key_rotation_share_identity() -> None:
    first = TheOddsApiProvider(
        "secret-a",
        sport="soccer_epl",
        bookmakers=("betfair", "pinnacle"),
        markets=("totals", "h2h"),
        event_ids=("event-b", "event-a"),
    )
    second = TheOddsApiProvider(
        "secret-b",
        sport="soccer_epl",
        regions=("us",),
        bookmakers=("pinnacle", "betfair"),
        markets=("h2h", "totals"),
        event_ids=("event-a", "event-b"),
        timeout_seconds=25,
    )

    # Explicit bookmakers are the effective provider scope; regions are not sent.
    assert first.source_id == second.source_id
    assert ":current:" in first.source_id
    assert "secret-a" not in first.source_id
    assert "secret-b" not in second.source_id


def test_equivalent_effective_scope_has_identical_url_evidence_and_durable_quote() -> None:
    payload = [
        {
            "id": "event-a",
            "sport_key": "soccer_epl",
            "commence_time": "2026-09-23T15:00:00Z",
            "bookmakers": [
                {
                    "key": "betfair",
                    "markets": [
                        {
                            "key": "h2h",
                            "last_update": "2026-09-23T11:55:00Z",
                            "outcomes": [{"name": "Draw", "price": "3.10"}],
                        }
                    ],
                }
            ],
        }
    ]
    first_urls: list[str] = []
    second_urls: list[str] = []

    def first_transport(url: str, _timeout: float) -> HttpJsonResponse:
        first_urls.append(url)
        return HttpJsonResponse(payload, 200, {})

    def second_transport(url: str, _timeout: float) -> HttpJsonResponse:
        second_urls.append(url)
        return HttpJsonResponse(payload, 200, {})

    first = TheOddsApiProvider(
        "same-secret",
        sport="soccer_epl",
        regions=("eu",),
        bookmakers=("pinnacle", "betfair"),
        markets=("totals", "h2h"),
        event_ids=("event-b", "event-a"),
        transport=first_transport,
        clock=_clock,
    )
    second = TheOddsApiProvider(
        "same-secret",
        sport="soccer_epl",
        regions=("us", "eu"),
        bookmakers=("betfair", "pinnacle"),
        markets=("h2h", "totals"),
        event_ids=("event-a", "event-b"),
        transport=second_transport,
        clock=_clock,
    )

    first_quote = first.read_batch().quotes[0]
    second_quote = second.read_batch().quotes[0]
    assert first.source_id == second.source_id
    assert first_urls == second_urls
    assert first.last_request_evidence is not None
    assert second.last_request_evidence is not None
    assert first.last_request_evidence.regions == ()
    assert second.last_request_evidence.regions == ()
    assert (
        first.last_request_evidence.source_metadata()
        == second.last_request_evidence.source_metadata()
    )
    assert first_quote.metadata["request"] == second_quote.metadata["request"]

    normalizer = CanonicalNormalizer()
    first_event = normalizer.normalize(first.source_id, first_quote)
    second_event = normalizer.normalize(second.source_id, second_quote)
    with tempfile.TemporaryDirectory() as directory:
        store = SQLiteMarketStore(Path(directory) / "market.db")
        try:
            assert store.append(first_event) is True
            assert store.append(second_event) is False
            assert len(store.events()) == 1
        finally:
            store.close()


def test_historical_stream_is_distinct_and_binds_requested_cutoff() -> None:
    first_provider = TheOddsApiProvider(
        "synthetic-secret",
        sport="soccer_epl",
        transport=_historical_transport(
            snapshot_at="2026-09-23T10:55:00Z",
            next_snapshot_at="2026-09-23T11:05:00Z",
        ),
        clock=_clock,
    )
    first = first_provider.read_historical_snapshot("2026-09-23T11:00:00Z")

    second_provider = TheOddsApiProvider(
        "synthetic-secret",
        sport="soccer_epl",
        transport=_historical_transport(
            snapshot_at="2026-09-23T11:05:00Z",
            next_snapshot_at="2026-09-23T11:15:00Z",
        ),
        clock=_clock,
    )
    second = second_provider.read_historical_snapshot("2026-09-23T11:10:00Z")

    assert first.batch.source_id != first_provider.source_id
    assert first.batch.source_id != second.batch.source_id
    assert ":historical:" in first.batch.source_id


def test_historical_cutoff_identity_normalizes_timezone_aliases() -> None:
    first_provider = TheOddsApiProvider(
        "synthetic-secret",
        sport="soccer_epl",
        transport=_historical_transport(
            snapshot_at="2026-09-23T10:55:00Z",
            next_snapshot_at="2026-09-23T11:05:00Z",
        ),
        clock=_clock,
    )
    first = first_provider.read_historical_snapshot("2026-09-23T11:00:00Z")

    alias_provider = TheOddsApiProvider(
        "synthetic-secret",
        sport="soccer_epl",
        transport=_historical_transport(
            snapshot_at="2026-09-23T10:55:00Z",
            next_snapshot_at="2026-09-23T11:05:00Z",
        ),
        clock=_clock,
    )
    alias = alias_provider.read_historical_snapshot("2026-09-23T13:00:00+02:00")

    assert first.batch.source_id == alias.batch.source_id


def test_request_scope_fields_are_read_only_after_construction() -> None:
    provider = TheOddsApiProvider(
        "synthetic-secret",
        sport="soccer_epl",
        regions=("eu",),
        markets=("h2h", "totals"),
        event_ids=("event-a",),
        include_sids=True,
        include_bet_limits=False,
    )
    original_source_id = provider.source_id

    mutations = (
        ("sport", "basketball_nba"),
        ("regions", ("us",)),
        ("bookmakers", ("pinnacle",)),
        ("markets", ("h2h",)),
        ("event_ids", ("event-b",)),
        ("include_sids", False),
        ("include_bet_limits", True),
    )
    for field, value in mutations:
        try:
            setattr(provider, field, value)
        except AttributeError:
            pass
        else:
            raise AssertionError(f"{field} request scope must be read-only")

    assert provider.sport == "soccer_epl"
    assert provider.regions == ("eu",)
    assert provider.bookmakers == ()
    assert provider.markets == ("h2h", "totals")
    assert provider.event_ids == ("event-a",)
    assert provider.include_sids is True
    assert provider.include_bet_limits is False
    assert provider.source_id == original_source_id
