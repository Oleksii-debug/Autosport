from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .localization import text
from .price_truth import market_price_truth_from_run_summary
from .session import ObservationResult, SessionResult


def result_summary(result: SessionResult) -> str:
    return text(
        "ui.result.summary",
        run_id=result.replay.run_id[:8],
        event_count=result.replay.event_count,
        balance=result.balance,
        net_profit=result.evaluation.net_profit,
        settled=len(result.settled_ticket_ids),
        portfolio_mode=result.portfolio.mode,
        worst=result.portfolio.worst_case,
        best=result.portfolio.best_case,
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


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number: {value}")
    return parsed


def _market_price_truth_line(result: SessionResult) -> str:
    try:
        text_payload = Path(result.result_path).read_text(encoding="utf-8")
        payload = json.loads(
            text_payload,
            object_pairs_hook=_reject_duplicate_object_pairs,
            parse_constant=_reject_nonfinite_json_constant,
            parse_float=_parse_finite_json_float,
        )
    except (OSError, UnicodeError):
        return text("ui.price_truth.error.missing")
    except RecursionError:
        return text("ui.price_truth.error.depth")
    except json.JSONDecodeError as exc:
        return text("ui.price_truth.error.json", detail=exc.msg)
    except ValueError as exc:
        return text("ui.price_truth.error.ambiguous", detail=exc)

    if not isinstance(payload, dict):
        return text("ui.price_truth.error.root")
    try:
        truth = market_price_truth_from_run_summary(payload)
    except ValueError as exc:
        return text("ui.price_truth.error.explicit", detail=exc)

    executable = str(truth.executable_quote_verified).lower()
    fill_fidelity = str(truth.paper_fill_fidelity_verified).lower()
    if "last_traded" in truth.price_semantics:
        return text(
            "ui.price_truth.betfair_last_traded",
            executable=executable,
            fill_fidelity=fill_fidelity,
        )
    return text(
        "ui.price_truth.generic",
        price_semantics=truth.price_semantics,
        executable=executable,
        fill_fidelity=fill_fidelity,
    )


def evaluation_lines(result: SessionResult) -> list[str]:
    """Human-readable evaluation and portfolio evidence for the keyboard/NVDA UI."""
    evaluation = result.evaluation
    portfolio = result.portfolio
    mode_truth = text(
        "ui.portfolio.mode.exact"
        if portfolio.mode == "exact"
        else "ui.portfolio.mode.approximate"
    )
    return [
        text(
            "ui.evaluation.replay",
            run_id=result.replay.run_id[:8],
            event_count=result.replay.event_count,
            settled_count=len(result.settled_ticket_ids),
        ),
        text(
            "ui.evaluation.bankroll",
            initial_bankroll=evaluation.initial_bankroll,
            final_balance=evaluation.final_balance,
            committed_stake=evaluation.committed_stake,
            settled_stake=evaluation.settled_stake,
        ),
        text(
            "ui.evaluation.metrics",
            net_profit=evaluation.net_profit,
            roi=evaluation.roi,
            won=evaluation.won,
            lost=evaluation.lost,
            void=evaluation.void,
        ),
        text(
            "ui.evaluation.portfolio",
            mode_truth=mode_truth,
            scenario_count=portfolio.scenario_count,
            worst=portfolio.worst_case,
            best=portfolio.best_case,
            mean=portfolio.mean_case,
        ),
        _market_price_truth_line(result),
        text("ui.evaluation.truth"),
    ]


def ticket_lines(session) -> list[str]:
    lines: list[str] = []
    for ticket in session.book.tickets.values():
        legs = ", ".join(
            f"{leg.event_id}/{leg.market_id}/{leg.selection_id}@{leg.locked_odds}"
            for leg in ticket.legs
        )
        lines.append(
            text(
                "ui.ticket.row",
                status=ticket.status.value.upper(),
                stake=ticket.stake,
                odds=ticket.combined_odds,
                payout=ticket.payout,
                legs=legs,
            )
        )
    return lines or [text("ui.ticket.empty")]


def observation_summary(result: ObservationResult) -> str:
    flags = ", ".join(result.stats.quality_flags) if result.stats.quality_flags else text("ui.observation.no_flags")
    return text(
        "ui.observation.summary",
        source_id=result.stats.source_id,
        health=result.health.status,
        received=result.stats.received,
        accepted=result.stats.accepted,
        rejected=result.stats.rejected,
        current=len(result.current_quotes),
        quality_flags=flags,
    )


def observation_quote_lines(result: ObservationResult) -> list[str]:
    lines = [
        text(
            "ui.observation.quote",
            event_id=event.event_id,
            market_type=event.market_type.value,
            market_id=event.market_id,
            selection_id=event.selection_id,
            odds=event.decimal_odds,
            source_time=event.source_ts or text("ui.observation.unknown_time"),
        )
        for event in result.current_quotes
    ]
    return lines or [text("ui.observation.empty")]
