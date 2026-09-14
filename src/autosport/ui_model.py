from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .price_truth import market_price_truth_from_run_summary
from .session import ObservationResult, SessionResult


def result_summary(result: SessionResult) -> str:
    return (
        f"Replay {result.replay.run_id[:8]}: events={result.replay.event_count}; "
        f"balance={result.balance}; net={result.evaluation.net_profit}; "
        f"settled={len(result.settled_ticket_ids)}; portfolio={result.portfolio.mode}; "
        f"worst={result.portfolio.worst_case}; best={result.portfolio.best_case}."
    )


def _reject_duplicate_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate object key: {key}")
        payload[key] = value
    return payload


def _reject_nonfinite_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {value}")


def _market_price_truth_line(result: SessionResult) -> str:
    try:
        text = Path(result.result_path).read_text(encoding="utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_object_pairs,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except (OSError, UnicodeError):
        return "Price truth | ERROR — run summary evidence is missing or unreadable."
    except RecursionError:
        return "Price truth | ERROR — run summary JSON nesting is too deep."
    except json.JSONDecodeError as exc:
        return f"Price truth | ERROR — run summary JSON is invalid: {exc.msg}."
    except ValueError as exc:
        return f"Price truth | ERROR — run summary JSON is ambiguous or non-canonical: {exc}."

    if not isinstance(payload, dict):
        return "Price truth | ERROR — run summary root is not an object."
    try:
        truth = market_price_truth_from_run_summary(payload)
    except ValueError as exc:
        return f"Price truth | ERROR — invalid explicit run truth: {exc}."

    executable = str(truth.executable_quote_verified).lower()
    fill_fidelity = str(truth.paper_fill_fidelity_verified).lower()
    if "last_traded" in truth.price_semantics:
        return (
            "Price truth | Betfair last-traded/last-matched observation; "
            f"executable quote verified={executable}; paper fill fidelity verified={fill_fidelity}."
        )
    return (
        f"Price truth | {truth.price_semantics}; executable quote verified={executable}; "
        f"paper fill fidelity verified={fill_fidelity}."
    )


def evaluation_lines(result: SessionResult) -> list[str]:
    """Human-readable evaluation and portfolio evidence for the keyboard/NVDA UI."""
    evaluation = result.evaluation
    portfolio = result.portfolio
    mode_truth = (
        "exact — усі релевантні сценарії цього portfolio report перебрано"
        if portfolio.mode == "exact"
        else "approximate — сценарії sampled; гарантії worst/best не заявляються"
    )
    return [
        (
            f"Replay {result.replay.run_id[:8]} | events {result.replay.event_count} | "
            f"settled tickets {len(result.settled_ticket_ids)}"
        ),
        (
            f"Bankroll | initial {evaluation.initial_bankroll} | final {evaluation.final_balance} | "
            f"committed {evaluation.committed_stake} | settled stake {evaluation.settled_stake}"
        ),
        (
            f"Evaluation | net {evaluation.net_profit} | ROI {evaluation.roi} | "
            f"won {evaluation.won} | lost {evaluation.lost} | void {evaluation.void}"
        ),
        (
            f"Portfolio | {mode_truth} | scenarios {portfolio.scenario_count} | "
            f"worst {portfolio.worst_case} | best {portfolio.best_case} | mean {portfolio.mean_case}"
        ),
        _market_price_truth_line(result),
        "Truth | paper simulation only; ця evaluation не є доказом майбутньої profitability.",
    ]


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
