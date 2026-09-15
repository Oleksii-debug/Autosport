from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from decimal import Decimal
from time import perf_counter_ns
from typing import Callable, Sequence

from autosport.domain import PaperTicket, TicketLeg
from autosport.portfolio import PortfolioEngine, PortfolioReport


_TARGET_P95_MS = 100.0
_COMPANION_KEYS_PER_EVENT = 3


@dataclass(frozen=True, slots=True)
class PortfolioRecomputeLatencyResult:
    total_tickets: int
    event_count: int
    tickets_per_event: int
    measured_updates: int
    warmup_updates: int
    affected_tickets_per_update: int
    scenarios_per_update: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _nearest_rank_percentile_ms(samples_ns: Sequence[int], percentile: float) -> float:
    if not samples_ns:
        raise ValueError("latency samples must not be empty")
    if isinstance(percentile, bool) or not isinstance(percentile, (int, float)):
        raise ValueError("percentile must be a finite number in (0, 100]")
    percentile_value = float(percentile)
    if not math.isfinite(percentile_value) or percentile_value <= 0 or percentile_value > 100:
        raise ValueError("percentile must be a finite number in (0, 100]")

    normalized: list[int] = []
    for sample in samples_ns:
        if isinstance(sample, bool) or not isinstance(sample, int) or sample <= 0:
            raise ValueError("latency samples must be positive integer nanoseconds")
        normalized.append(sample)

    ordered = sorted(normalized)
    rank = math.ceil((percentile_value / 100.0) * len(ordered))
    return ordered[rank - 1] / 1_000_000.0


def _build_portfolio(event_count: int, tickets_per_event: int) -> tuple[list[PaperTicket], list[str]]:
    event_count = _positive_int("event_count", event_count)
    tickets_per_event = _positive_int("tickets_per_event", tickets_per_event)
    tickets: list[PaperTicket] = []
    trigger_quote_keys: list[str] = []
    for event_index in range(event_count):
        event_id = f"event-{event_index}"
        trigger_leg = TicketLeg(event_id, "winner", "home", Decimal("1.80"))
        trigger_quote_keys.append(trigger_leg.quote_key)
        for ticket_index in range(tickets_per_event):
            companion_index = ticket_index % min(_COMPANION_KEYS_PER_EVENT, tickets_per_event)
            companion_leg = TicketLeg(
                event_id,
                f"aux-{companion_index}",
                "yes",
                Decimal("1.50") + Decimal(companion_index) / Decimal("10"),
            )
            tickets.append(
                PaperTicket(
                    ticket_id=f"ticket-{event_index}-{ticket_index}",
                    stake=Decimal("10"),
                    legs=(trigger_leg, companion_leg),
                    placed_at="2026-01-01T00:00:00+00:00",
                    strategy_reason="portfolio recompute benchmark fixture",
                )
            )
    return tickets, trigger_quote_keys


def _expected_scenarios(tickets_per_event: int) -> int:
    tickets_per_event = _positive_int("tickets_per_event", tickets_per_event)
    distinct_keys = 1 + min(_COMPANION_KEYS_PER_EVENT, tickets_per_event)
    return 2**distinct_keys


def _validate_recompute(
    *,
    affected_ids: list[str],
    expected_ticket_ids: frozenset[str],
    report: PortfolioReport,
    tickets_per_event: int,
    expected_scenarios: int,
) -> None:
    observed_ticket_ids = set(affected_ids)
    if len(affected_ids) != tickets_per_event or len(observed_ticket_ids) != tickets_per_event:
        raise RuntimeError(
            "affected-subgraph discovery was incomplete or duplicated: "
            f"expected={tickets_per_event} observed={len(affected_ids)} unique={len(observed_ticket_ids)}"
        )
    if observed_ticket_ids != expected_ticket_ids:
        raise RuntimeError(
            "affected-subgraph discovery returned wrong ticket identities for the trigger: "
            f"expected={sorted(expected_ticket_ids)!r} observed={sorted(observed_ticket_ids)!r}"
        )
    if report.mode != "exact":
        raise RuntimeError(f"ordinary benchmark subgraph unexpectedly used mode={report.mode!r}")
    if report.scenario_count != expected_scenarios:
        raise RuntimeError(
            "ordinary benchmark subgraph scenario count changed: "
            f"expected={expected_scenarios} observed={report.scenario_count}"
        )


def run_latency_benchmark(
    *,
    event_count: int = 100,
    tickets_per_event: int = 24,
    measured_updates: int = 1_000,
    warmup_updates: int = 100,
    clock_ns: Callable[[], int] = perf_counter_ns,
) -> PortfolioRecomputeLatencyResult:
    """Measure current affected-ticket discovery + exact affected-subgraph analysis.

    The workload and timing scope are deterministic, but the measured latency is machine
    specific. This harness never turns a CI result into the target-Windows-laptop claim.
    """

    event_count = _positive_int("event_count", event_count)
    tickets_per_event = _positive_int("tickets_per_event", tickets_per_event)
    measured_updates = _positive_int("measured_updates", measured_updates)
    warmup_updates = _nonnegative_int("warmup_updates", warmup_updates)

    tickets, trigger_quote_keys = _build_portfolio(event_count, tickets_per_event)
    by_id = {ticket.ticket_id: ticket for ticket in tickets}
    if len(by_id) != len(tickets):
        raise RuntimeError("benchmark fixture contains duplicate ticket identities")

    expected_ids_by_trigger = {
        trigger_quote_key: frozenset(
            ticket.ticket_id
            for ticket in tickets
            if any(leg.quote_key == trigger_quote_key for leg in ticket.legs)
        )
        for trigger_quote_key in trigger_quote_keys
    }
    if any(len(expected_ids) != tickets_per_event for expected_ids in expected_ids_by_trigger.values()):
        raise RuntimeError("benchmark fixture trigger mapping did not produce the exact expected ticket set")

    expected_scenarios = _expected_scenarios(tickets_per_event)
    engine = PortfolioEngine(max_exact_states=max(100_000, expected_scenarios))

    def recompute(trigger_quote_key: str) -> tuple[list[str], PortfolioReport]:
        affected_ids = engine.affected_tickets(tickets, trigger_quote_key)
        try:
            affected = [by_id[ticket_id] for ticket_id in affected_ids]
        except KeyError as exc:
            raise RuntimeError("affected-ticket identity was not present in the benchmark fixture") from exc
        return affected_ids, engine.analyse(affected)

    for update_index in range(warmup_updates):
        trigger_quote_key = trigger_quote_keys[update_index % event_count]
        affected_ids, report = recompute(trigger_quote_key)
        _validate_recompute(
            affected_ids=affected_ids,
            expected_ticket_ids=expected_ids_by_trigger[trigger_quote_key],
            report=report,
            tickets_per_event=tickets_per_event,
            expected_scenarios=expected_scenarios,
        )

    samples_ns: list[int] = []
    for update_index in range(measured_updates):
        trigger_quote_key = trigger_quote_keys[update_index % event_count]
        started_ns = clock_ns()
        affected_ids, report = recompute(trigger_quote_key)
        elapsed_ns = clock_ns() - started_ns
        if isinstance(elapsed_ns, bool) or not isinstance(elapsed_ns, int) or elapsed_ns <= 0:
            raise RuntimeError("latency clock did not advance for a measured portfolio recompute")
        _validate_recompute(
            affected_ids=affected_ids,
            expected_ticket_ids=expected_ids_by_trigger[trigger_quote_key],
            report=report,
            tickets_per_event=tickets_per_event,
            expected_scenarios=expected_scenarios,
        )
        samples_ns.append(elapsed_ns)

    if len(samples_ns) != measured_updates:
        raise RuntimeError(
            "portfolio recompute workload did not complete exactly: "
            f"requested={measured_updates} samples={len(samples_ns)}"
        )

    p50_ms = _nearest_rank_percentile_ms(samples_ns, 50)
    p95_ms = _nearest_rank_percentile_ms(samples_ns, 95)
    p99_ms = _nearest_rank_percentile_ms(samples_ns, 99)
    max_ms = max(samples_ns) / 1_000_000.0
    if not (0 < p50_ms <= p95_ms <= p99_ms <= max_ms):
        raise RuntimeError("portfolio recompute latency summary ordering is invalid")

    return PortfolioRecomputeLatencyResult(
        total_tickets=len(tickets),
        event_count=event_count,
        tickets_per_event=tickets_per_event,
        measured_updates=measured_updates,
        warmup_updates=warmup_updates,
        affected_tickets_per_update=tickets_per_event,
        scenarios_per_update=expected_scenarios,
        p50_ms=p50_ms,
        p95_ms=p95_ms,
        p99_ms=p99_ms,
        max_ms=max_ms,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure Autosport affected-ticket discovery plus exact affected-subgraph portfolio analysis. "
            "The observed result is machine-specific evidence and is not a release target claim by itself."
        )
    )
    parser.add_argument("--events", type=int, default=100, help="independent event subgraphs in the portfolio")
    parser.add_argument("--tickets-per-event", type=int, default=24, help="open tickets in each affected subgraph")
    parser.add_argument("--updates", type=int, default=1_000, help="measured affected-subgraph recomputes")
    parser.add_argument("--warmup", type=int, default=100, help="unmeasured warmup recomputes")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = run_latency_benchmark(
        event_count=args.events,
        tickets_per_event=args.tickets_per_event,
        measured_updates=args.updates,
        warmup_updates=args.warmup,
    )
    print(
        "scope=affected_ticket_discovery_plus_exact_subgraph_analysis "
        "target_machine_required=true target_claim=false "
        f"target_p95_ms={_TARGET_P95_MS:.0f} "
        f"events={result.event_count} total_tickets={result.total_tickets} "
        f"tickets_per_event={result.tickets_per_event} affected_per_update={result.affected_tickets_per_update} "
        f"scenarios_per_update={result.scenarios_per_update} measured_updates={result.measured_updates} "
        f"warmup_updates={result.warmup_updates} p50_ms={result.p50_ms:.3f} "
        f"p95_ms={result.p95_ms:.3f} p99_ms={result.p99_ms:.3f} max_ms={result.max_ms:.3f}"
    )


if __name__ == "__main__":
    main()
