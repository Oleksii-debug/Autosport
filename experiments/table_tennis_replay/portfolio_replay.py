"""Exploratory assumed-fill replay of recorded live two-match paper parlays.

No provider writes, live execution, learner training or production book mutation.
Recorded quotes and observed result times are not verified bookmaker fills.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy
from h2h_exploratory import instant, result

INITIAL = Decimal("100")
STAKE = Decimal("1")
FEE = Decimal("0.01")
MAX_AGE = timedelta(seconds=90)
COOLDOWN = timedelta(seconds=180)


def _price(value, polled, now):
    if not isinstance(value, dict) or value.get("stale") is not False or value.get("suspended") is not False:
        return None
    raw = value.get("odds")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        price = Decimal(str(raw))
        checked = instant(value.get("market_checked_at"))
        changed = instant(value.get("source_price_changed_at"))
        if not price.is_finite() or price <= 1 or not (changed <= checked <= polled <= now):
            return None
        if now - checked > MAX_AGE:
            return None
        return price
    except (InvalidOperation, ValueError):
        return None


def _eligible(match, frame, observed, now):
    if frame.get("kind") != "live" or frame.get("source_stale") is not False:
        return None
    if (frame.get("source_id") != match.get("id")
            or match.get("live_id") != match.get("id")):
        return None
    try:
        polled = instant(frame.get("source_polled_at"))
        last_live = instant(frame.get("source_last_live_seen"))
        if not (last_live <= polled <= observed <= now):
            return None
        if now - observed > MAX_AGE or now - last_live > MAX_AGE:
            return None
        odds = frame.get("odds") or {}
        home = _price(odds.get("home"), polled, now)
        away = _price(odds.get("away"), polled, now)
        if home is None or away is None:
            return None
        side = "home" if home <= away else "away"
        return side, home if side == "home" else away
    except (ValueError, AttributeError):
        return None


def replay(data, *, include_scenario_stress=False, include_single_void_stress=False,
           include_alias_sensitivity=False, include_probability_sensitivity=False,
           include_joint_probability_bounds=False, include_alias_probability_bounds=False):
    if type(include_scenario_stress) is not bool:
        raise ValueError("include_scenario_stress must be a boolean")
    if type(include_single_void_stress) is not bool:
        raise ValueError("include_single_void_stress must be a boolean")
    if type(include_alias_sensitivity) is not bool:
        raise ValueError("include_alias_sensitivity must be a boolean")
    if type(include_probability_sensitivity) is not bool:
        raise ValueError("include_probability_sensitivity must be a boolean")
    if type(include_joint_probability_bounds) is not bool:
        raise ValueError("include_joint_probability_bounds must be a boolean")
    if type(include_alias_probability_bounds) is not bool:
        raise ValueError("include_alias_probability_bounds must be a boolean")
    matches = data["matches"]
    if not isinstance(matches, list):
        raise ValueError("matches must be a list")
    by_id = {}
    timeline = defaultdict(lambda: {"frames": [], "results": []})
    for match in matches:
        identity = match.get("id")
        if not isinstance(identity, str) or not identity or identity.strip() != identity or "|" in identity or identity in by_id:
            raise ValueError("missing, invalid or duplicate fixture ID")
        by_id[identity] = match
        seen = set()
        for frame in match.get("frames", []):
            if frame.get("kind") != "live":
                continue
            observed = instant(frame.get("observed_at"))
            if observed in seen:
                raise ValueError("duplicate frame timestamp for fixture")
            seen.add(observed)
            timeline[observed]["frames"].append((identity, frame))
        raw_result = match.get("result")
        if raw_result:
            # An unparseable result time is not an outcome. Do not manufacture one.
            try:
                observed = instant(raw_result.get("observed_at"))
            except (ValueError, AttributeError):
                continue
            parsed = result(match)
            source_id = raw_result.get("source_fixture_id")
            # Cross-ID reconciliation lacks a decision-time receipt here.
            try:
                first_live = instant(match.get("first_live_at"))
                last_seen = instant(raw_result.get("source_finished_at"))
                periods = raw_result.get("periods") or []
                same_scores = raw_result.get("final_score") == ",".join(p["score"] for p in periods)
                if (source_id != identity or not (first_live <= last_seen <= observed)
                        or not same_scores):
                    parsed = None
            except (ValueError, TypeError, KeyError):
                parsed = None
            timeline[observed]["results"].append((identity, None if parsed is None else parsed[0]))

    book, risk = PaperBook(INITIAL), PaperRiskPolicy()
    latest, known, closed, tickets = {}, {}, set(), []
    opened = won = lost = risk_rejected = 0
    last_open = None
    peak, max_drawdown, peak_committed = INITIAL, Decimal(0), Decimal(0)
    max_shared_fixture_stake = Decimal(0)
    max_tickets_sharing_fixture = 0
    peak_snapshots = {"open": [], "shared": []}
    peak_snapshot_open = peak_snapshot_shared = 0
    log = []

    def measure():
        nonlocal peak, max_drawdown, peak_committed, max_shared_fixture_stake, max_tickets_sharing_fixture
        nonlocal peak_snapshot_open, peak_snapshot_shared
        exposure = Counter()
        for ticket in tickets:
            if ticket.status is TicketStatus.OPEN:
                for leg in ticket.legs:
                    exposure[leg.event_id] += 1
        if exposure:
            max_tickets_sharing_fixture = max(max_tickets_sharing_fixture, max(exposure.values()))
            max_shared_fixture_stake = max(max_shared_fixture_stake, STAKE * max(exposure.values()))
        if (include_scenario_stress or include_single_void_stress or include_alias_sensitivity
                or include_probability_sensitivity or include_joint_probability_bounds
                or include_alias_probability_bounds):
            # Copy only at a new strict maximum: ties retain the first state.
            # Snapshots remain internal; the public report contains aggregates.
            from autosport.portfolio import _snapshot_open_tickets_for_analysis
            n_open = sum(ticket.status is TicketStatus.OPEN for ticket in tickets)
            n_shared = max(exposure.values(), default=0)
            if n_open > peak_snapshot_open:
                peak_snapshots["open"] = _snapshot_open_tickets_for_analysis(tickets)
                peak_snapshot_open = n_open
            if n_shared > peak_snapshot_shared:
                peak_snapshots["shared"] = _snapshot_open_tickets_for_analysis(tickets)
                peak_snapshot_shared = n_shared
        committed = book.committed_stake
        peak_committed = max(peak_committed, committed)
        equity = book.balance + committed - FEE * opened
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    for now in sorted(timeline):
        batch = timeline[now]
        for identity, frame in sorted(batch["frames"], key=lambda item: item[0]):
            if identity not in closed:
                # Even an invalid/suspended frame replaces earlier market state.
                latest[identity] = (frame, now)
        if batch["frames"] and (last_open is None or now - last_open >= COOLDOWN):
            choices = []
            for identity in sorted(latest):
                frame, seen_at = latest[identity]
                candidate = _eligible(by_id[identity], frame, seen_at, now)
                if identity not in closed and candidate is not None:
                    choices.append((identity, *candidate))
            if len(choices) >= 2:
                selected = choices[:2]
                decision = risk.evaluate(book, STAKE)
                # Fee is separate from PaperBook, so preserve the stressed cash reserve.
                reserve_ok = book.balance - FEE * (opened + 1) - STAKE >= INITIAL * risk.minimum_cash_reserve_fraction
                if decision.allowed and reserve_ok:
                    legs = tuple(TicketLeg(identity, "match_winner", side, price, sport="table_tennis")
                                 for identity, side, price in selected)
                    ticket = book.open_ticket(legs, STAKE, reason="hypothetical recorded quotes; no accepted fill", placed_at=now.isoformat())
                    tickets.append(ticket)
                    opened += 1
                    last_open = now
                    log.append({"at": now.isoformat(), "action": "open", "legs": [{"fixture": i, "side": s, "odds": str(p)} for i, s, p in selected]})
                    measure()
                else:
                    risk_rejected += 1
        # Same-time outcomes come after opening and cannot fund the decision.
        for identity, winner in sorted(batch["results"], key=lambda item: item[0]):
            closed.add(identity)
            latest.pop(identity, None)
            if winner is not None:
                known[identity] = "home" if winner == 0 else "away"
        for ticket in tickets:
            if ticket.status is not TicketStatus.OPEN or not all(leg.event_id in known for leg in ticket.legs):
                continue
            winners = {leg.quote_key for leg in ticket.legs if known[leg.event_id] == leg.selection_id}
            book.settle(ticket.ticket_id, winners, settled_at=now.isoformat())
            won += int(ticket.status is TicketStatus.WON)
            lost += int(ticket.status is TicketStatus.LOST)
            log.append({"at": now.isoformat(), "action": "settle", "status": ticket.status.value})
            measure()
    committed = book.committed_stake
    equity = book.balance + committed
    report = {"schema": "autosport.tt_exploratory_portfolio.v1", "fixtures": len(matches),
            "opened": opened, "won": won, "lost": lost, "pending": opened - won - lost,
            "risk_rejected": risk_rejected, "cash_units_before_cost": str(book.balance),
            "open_stake_units": str(committed), "max_open_stake_units": str(peak_committed),
            "max_tickets_sharing_fixture": max_tickets_sharing_fixture,
            "max_stake_sharing_fixture_units": str(max_shared_fixture_stake),
            "gross_net_assuming_open_stake_at_face_value": str(equity - INITIAL),
            "cost_stressed_net_assuming_open_stake_at_face_value": str(equity - FEE * opened - INITIAL),
            "cost_stressed_max_drawdown_stake_at_face_value": str(max_drawdown),
            "open_stakes_are_not_marked_to_market": True,
            "quotes_are_assumed_fills": True, "real_money_execution": False,
            "independent_holdout": False, "events": log}
    if include_scenario_stress:
        from portfolio_scenario_stress import summarize_snapshot
        report["scenario_stress"] = {
            "selection": "first occurrence of each maximum, selected retrospectively",
            "first_peak_open_tickets": summarize_snapshot(peak_snapshots["open"]),
            "first_peak_exact_id_sharing": summarize_snapshot(peak_snapshots["shared"]),
            "scenario_summary_contains_fixture_ids_or_names": False,
            "source_independence_verified": False, "profitability_claim": False}
    if include_single_void_stress:
        from portfolio_single_void_stress import summarize_single_void
        report["single_void_stress"] = {
            "selection": "first occurrence of each maximum, selected retrospectively",
            "first_peak_open_tickets": summarize_single_void(peak_snapshots["open"]),
            "first_peak_exact_id_sharing": summarize_single_void(peak_snapshots["shared"]),
            "scenario_summary_contains_fixture_ids_or_names": False,
            "source_independence_verified": False, "profitability_claim": False}
    if include_alias_sensitivity:
        from portfolio_alias_sensitivity import summarize_alias_sensitivity
        report["alias_sensitivity"] = {
            "selection": "first occurrence of each maximum, selected retrospectively",
            "first_peak_open_tickets": summarize_alias_sensitivity(peak_snapshots["open"]),
            "first_peak_exact_id_sharing": summarize_alias_sensitivity(peak_snapshots["shared"]),
            "scenario_summary_contains_fixture_ids_or_names": False,
            "source_independence_verified": False, "profitability_claim": False}
    if include_probability_sensitivity:
        from portfolio_probability_sensitivity import summarize_probability_sensitivity
        report["probability_sensitivity"] = {
            "selection": "first occurrence of each maximum, selected retrospectively",
            "first_peak_open_tickets": summarize_probability_sensitivity(peak_snapshots["open"]),
            "first_peak_exact_id_sharing": summarize_probability_sensitivity(peak_snapshots["shared"]),
            "scenario_summary_contains_fixture_ids_or_names": False,
            "source_independence_verified": False, "profitability_claim": False}
    if include_joint_probability_bounds:
        from portfolio_joint_probability_bounds import summarize_joint_bounds
        report["joint_probability_bounds"] = {
            "selection": "first occurrence of each maximum, selected retrospectively",
            "first_peak_open_tickets": summarize_joint_bounds(peak_snapshots["open"]),
            "first_peak_exact_id_sharing": summarize_joint_bounds(peak_snapshots["shared"]),
            "scenario_summary_contains_fixture_ids_or_names": False,
            "source_independence_verified": False, "profitability_claim": False}
    if include_alias_probability_bounds:
        from portfolio_alias_probability_bounds import summarize_alias_probability_bounds
        report["alias_probability_bounds"] = {
            "selection": "first occurrence of each maximum, selected retrospectively",
            "first_peak_open_tickets": summarize_alias_probability_bounds(peak_snapshots["open"]),
            "first_peak_exact_id_sharing": summarize_alias_probability_bounds(peak_snapshots["shared"]),
            "scenario_summary_contains_fixture_ids_or_names": False,
            "source_independence_verified": False, "profitability_claim": False}
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    report = replay(json.loads(raw))
    report["input_sha256"] = hashlib.sha256(raw).hexdigest()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
