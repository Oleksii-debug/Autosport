from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from autosport.localization import (
    CATALOG_VERSION,
    DEFAULT_LOCALE,
    catalog,
    require_keys,
    text,
)
from autosport.ui_model import (
    observation_quote_lines,
    observation_summary,
    result_summary,
    ticket_lines,
)


def test_catalog_is_versioned_ukrainian_default_and_fails_closed() -> None:
    assert DEFAULT_LOCALE == "uk-UA"
    assert CATALOG_VERSION == 1
    assert text("ui.ticket.empty") == "Паперові квитки ще відсутні."
    assert text("ui.boolean.true") == "так"
    assert text("ui.boolean.false") == "ні"

    require_keys(
        {
            "ui.result.summary",
            "ui.evaluation.bankroll",
            "ui.evaluation.metrics",
            "ui.evaluation.portfolio",
            "ui.evaluation.truth",
            "ui.ticket.row",
            "ui.observation.summary",
            "ui.observation.quote",
        }
    )

    with pytest.raises(ValueError, match="unsupported locale"):
        catalog("en-US")
    with pytest.raises(KeyError, match="missing localization key"):
        text("ui.missing")
    with pytest.raises(KeyError, match="missing localization value"):
        text("ui.result.summary", run_id="r")
    with pytest.raises(KeyError, match="missing localization keys"):
        require_keys({"ui.missing"})


def test_result_summary_localizes_labels_but_preserves_raw_economic_values() -> None:
    result = SimpleNamespace(
        replay=SimpleNamespace(run_id="abcdef123456", event_count=7),
        balance=Decimal("10000.25"),
        evaluation=SimpleNamespace(net_profit=Decimal("-12.50")),
        settled_ticket_ids=("ticket-1", "ticket-2"),
        portfolio=SimpleNamespace(
            mode="exact",
            worst_case=Decimal("-50.00"),
            best_case=Decimal("75.00"),
        ),
    )

    rendered = result_summary(result)

    assert rendered.startswith("Повтор abcdef12:")
    assert "подій=7" in rendered
    assert "баланс=10000.25" in rendered
    assert "чистий_результат=-12.50" in rendered
    assert "портфель=exact" in rendered
    assert "найгірше=-50.00" in rendered
    assert "найкраще=75.00" in rendered


def test_ticket_lines_preserve_canonical_leg_identity_and_decimal_values() -> None:
    ticket = SimpleNamespace(
        status=SimpleNamespace(value="won"),
        stake=Decimal("25.50"),
        combined_odds=Decimal("2.10"),
        payout=Decimal("53.55"),
        legs=(
            SimpleNamespace(
                event_id="event:raw-1",
                market_id="market:raw-2",
                selection_id="selection:raw-3",
                locked_odds=Decimal("2.10"),
            ),
        ),
    )
    session = SimpleNamespace(book=SimpleNamespace(tickets={"ticket-raw": ticket}))

    rendered = ticket_lines(session)

    assert len(rendered) == 1
    assert rendered[0].startswith("WON | ставка 25.50 | коефіцієнт 2.10 | виплата 53.55 | ")
    assert "event:raw-1/market:raw-2/selection:raw-3@2.10" in rendered[0]
    assert ticket_lines(SimpleNamespace(book=SimpleNamespace(tickets={}))) == [
        "Паперові квитки ще відсутні."
    ]


def test_observation_presentation_is_ukrainian_without_mutating_provider_identity() -> None:
    event = SimpleNamespace(
        event_id="evt-provider-1",
        market_type=SimpleNamespace(value="MATCH_WINNER"),
        market_id="market-provider-2",
        selection_id="selection-provider-3",
        decimal_odds=Decimal("1.91"),
        source_ts="2026-09-16T10:00:00Z",
    )
    result = SimpleNamespace(
        stats=SimpleNamespace(
            source_id="provider-raw-id",
            received=3,
            accepted=2,
            rejected=1,
            quality_flags=(),
        ),
        health=SimpleNamespace(status="HEALTHY"),
        current_quotes=(event,),
    )

    summary = observation_summary(result)
    quote = observation_quote_lines(result)[0]

    assert summary.startswith("Поточний знімок: джерело=provider-raw-id;")
    assert "отримано=3" in summary
    assert "прийнято=2" in summary
    assert "прапорці якості=немає" in summary
    assert "evt-provider-1 | MATCH_WINNER | market-provider-2 | selection-provider-3" in quote
    assert "коефіцієнт 1.91" in quote
    assert "час джерела 2026-09-16T10:00:00Z" in quote

    empty_result = SimpleNamespace(current_quotes=())
    assert observation_quote_lines(empty_result) == ["Поточні котирування ще відсутні."]
