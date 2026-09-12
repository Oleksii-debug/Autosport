from __future__ import annotations

from .session import ObservationResult, SessionResult


def result_summary(result: SessionResult) -> str:
    return (
        f"Replay {result.replay.run_id[:8]}: events={result.replay.event_count}; "
        f"balance={result.balance}; net={result.evaluation.net_profit}; "
        f"settled={len(result.settled_ticket_ids)}; portfolio={result.portfolio.mode}; "
        f"worst={result.portfolio.worst_case}; best={result.portfolio.best_case}."
    )


def ticket_lines(session) -> list[str]:
    lines: list[str] = []
    for ticket in session.book.tickets.values():
        legs = ", ".join(f"{leg.event_id}/{leg.market_id}/{leg.selection_id}@{leg.locked_odds}" for leg in ticket.legs)
        lines.append(
            f"{ticket.status.value.upper()} | stake {ticket.stake} | odds {ticket.combined_odds} | payout {ticket.payout} | {legs}"
        )
    return lines or ["Paper tickets ще відсутні."]


def observation_summary(result: ObservationResult) -> str:
    flags = ", ".join(result.stats.quality_flags) if result.stats.quality_flags else "немає"
    return (
        f"Live snapshot: source={result.stats.source_id}; health={result.health.status}; "
        f"received={result.stats.received}; accepted={result.stats.accepted}; "
        f"rejected={result.stats.rejected}; current={len(result.current_quotes)}; "
        f"quality flags={flags}."
    )


def observation_quote_lines(result: ObservationResult) -> list[str]:
    lines = [
        (
            f"{event.event_id} | {event.market_type.value} | {event.market_id} | "
            f"{event.selection_id} | odds {event.decimal_odds} | source time {event.source_ts or 'невідомий'}"
        )
        for event in result.current_quotes
    ]
    return lines or ["Live quotes ще відсутні."]
