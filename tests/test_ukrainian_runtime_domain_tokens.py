from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from autosport.domain import TicketStatus
from autosport.ui_model import observation_summary, ticket_lines


def _ticket(status: TicketStatus):
    leg = SimpleNamespace(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        locked_odds=Decimal("2.00"),
        sport="soccer",
    )
    return SimpleNamespace(
        status=status,
        stake=Decimal("10.00"),
        combined_odds=Decimal("2.00"),
        payout=Decimal("0"),
        legs=(leg,),
    )


def _observation(*, status: str, quality_flags: tuple[str, ...]):
    stats = SimpleNamespace(
        source_id="source-1",
        received=3,
        accepted=2,
        rejected=1,
        quality_flags=quality_flags,
    )
    health = SimpleNamespace(status=status)
    return SimpleNamespace(stats=stats, health=health, current_quotes=())


@pytest.mark.parametrize(
    ("status", "ukrainian"),
    [
        (TicketStatus.OPEN, "відкрито"),
        (TicketStatus.WON, "виграно"),
        (TicketStatus.LOST, "програно"),
        (TicketStatus.VOID, "повернено"),
    ],
)
def test_ticket_lines_localize_product_owned_ticket_status(
    status: TicketStatus,
    ukrainian: str,
) -> None:
    session = SimpleNamespace(
        book=SimpleNamespace(tickets={"ticket-1": _ticket(status)})
    )

    line = ticket_lines(session)[0]

    assert line.startswith(f"{ukrainian} |")
    assert status.value.upper() not in line


@pytest.mark.parametrize(
    ("status", "ukrainian"),
    [
        ("unknown", "невідомий"),
        ("healthy", "нормальний"),
        ("degraded", "погіршений"),
        ("failed", "помилка"),
    ],
)
def test_observation_summary_localizes_product_owned_health_status(
    status: str,
    ukrainian: str,
) -> None:
    summary = observation_summary(_observation(status=status, quality_flags=()))

    assert f"стан={ukrainian}" in summary
    assert f"стан={status}" not in summary


@pytest.mark.parametrize(
    ("flag", "ukrainian"),
    [
        ("INVALID_SOURCE_TIMESTAMP", "некоректний час джерела"),
        ("STALE_SOURCE", "застарілі дані джерела"),
        ("FUTURE_CLOCK_SKEW", "час джерела випереджає локальний годинник"),
        ("INVALID_QUOTE", "некоректне котирування"),
        ("SOURCE_TIME_REGRESSION", "час джерела рухається назад"),
    ],
)
def test_observation_summary_localizes_product_owned_ingestion_flags(
    flag: str,
    ukrainian: str,
) -> None:
    summary = observation_summary(
        _observation(status="degraded", quality_flags=(flag,))
    )

    assert ukrainian in summary
    assert flag not in summary


def test_observation_summary_preserves_unknown_provider_flag_verbatim() -> None:
    unknown_flag = "PROVIDER_CUSTOM_SIGNAL"

    summary = observation_summary(
        _observation(
            status="degraded",
            quality_flags=("STALE_SOURCE", unknown_flag),
        )
    )

    assert "застарілі дані джерела" in summary
    assert unknown_flag in summary
