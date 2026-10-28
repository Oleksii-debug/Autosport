"""Reconstruct an assumed-fill virtual portfolio from recorded-match outcomes.

This derived simulation is isolated from product financial authority and cannot
execute real money or promote a strategy. Captured source odds are not fills.
"""
from __future__ import annotations

import csv
from decimal import Decimal
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path

from .domain import TicketLeg
from .paper import PaperBook
from .prospective_shadow_observer import ObserverPlan, candidate
from .risk import PaperRiskPolicy
from .shadow_strategy_memory import ShadowStrategyMemory, digest, instant


INITIAL = Decimal("100")
STAKE = Decimal("1")
FEE = Decimal("0.01")


def _fee_reserve_allows(book, risk, opened):
    # Fees are not posted to the canonical book, so protect the stressed cash
    # reserve separately without weakening its existing risk decision.
    return book.balance - FEE * (opened + 1) - STAKE >= INITIAL * risk.minimum_cash_reserve_fraction


def project(root: Path):
    root = Path(root)
    wrapper = json.loads((root / "observer.json").read_text("utf-8"))
    state = wrapper["state"]
    if digest(state) != wrapper["sha256"]:
        raise ValueError("observer checkpoint integrity mismatch")
    plan = ObserverPlan(**state["plan"])
    memory = ShadowStrategyMemory.load(root / "memory.json")
    records = {r["event_id"]: r for r in memory.payload()["records"]}
    events = state["candidates"]
    if any(event.get("source_conflict") for event in events.values()):
        raise ValueError("source correction requires explicit review; portfolio held")
    settled_ids = {identity for identity, event in events.items() if event["state"] == "RESULT_RECORDED_NOT_EXECUTED"}
    if set(records) != settled_ids:
        raise ValueError("observer and shadow feedback differ; recover checkpoint first")

    @lru_cache(maxsize=128)
    def load_source(sha, suffix):
        if type(sha) is not str or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("invalid source digest")
        raw = (root / "raw" / (sha + suffix)).read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError("source snapshot digest mismatch")
        return raw

    @lru_cache(maxsize=128)
    def forecast_by_id(sha):
        doc = json.loads(load_source(sha, ".json"), parse_float=Decimal)
        items = doc["predictions"]
        mapping = {str(item["id"]): item for item in items}
        if len(mapping) != len(items):
            raise ValueError("duplicate forecast source identity")
        return doc["generated_at"], mapping

    @lru_cache(maxsize=128)
    def outcomes_by_id(sha):
        rows = list(csv.DictReader(io.StringIO(load_source(sha, ".csv").decode("utf-8-sig"))))
        mapping = {row["id"]: row for row in rows}
        if len(mapping) != len(rows):
            raise ValueError("duplicate result source identity")
        return mapping

    actions = []
    for identity, event in events.items():
        if event.get("event_id") != identity or event.get("memory_preference") not in {"WAIT", "PRICE_GAP"}:
            raise ValueError("invalid frozen candidate identity or action")
        generated_at, source_items = forecast_by_id(event["forecast_sha256"])
        captured = source_items.get(identity)
        if captured is None:
            raise ValueError("candidate missing from recorded forecast")
        decision_at = instant(event["decision_at"])
        if not 0 <= (decision_at - instant(generated_at)).total_seconds() <= plan.maximum_age_seconds or decision_at > instant(state["last_received_at"]):
            raise ValueError("forecast or decision chronology invalid")
        recomputed = candidate(captured, received_at=event["decision_at"], plan=plan)
        if recomputed is None or any(event.get(key) != value for key, value in recomputed.items() if key != "state"):
            raise ValueError("candidate differs from recorded forecast")
        if event["state"] not in {"PENDING_RESULT_NOT_EXECUTED", "RESULT_RECORDED_NOT_EXECUTED"}:
            raise ValueError("invalid candidate state")
        actions.append((instant(event["decision_at"]), 0, instant(event["kickoff_utc"]), identity, event))
        if identity in settled_ids:
            row = outcomes_by_id(event["result_sha256"]).get(identity)
            if (not row or row.get("settled") != "1" or row.get("result") not in {"1", "X", "2"}
                    or row["result"] != event["result"] or any(row.get(key) != event[key] for key in ("home", "away", "league"))
                    or instant(row["kickoff_utc"]) != instant(event["kickoff_utc"])):
                raise ValueError("recorded result identity or outcome mismatch")
            received = instant(event["result_first_received_at"])
            if received > instant(state["last_received_at"]) or (received - instant(event["kickoff_utc"])).total_seconds() < 7200:
                raise ValueError("outcome arrived before permitted settlement time")
            raw_reward = Decimal(event["observed_odds"]) - 1 if event["selection"] == event["result"] else Decimal(-1)
            net = raw_reward - Decimal(plan.cost_bps) / 10000
            record = records[identity]
            if (Decimal(event["hypothetical_net_return"]) != net
                    or Decimal(record["net_returns"]["PRICE_GAP"]) != net
                    or record["source_sha256"] != event["result_sha256"]
                    or instant(record["result_available_at"]) != received
                    or instant(record["decision_at"]) != instant(event["decision_at"])):
                raise ValueError("recorded result and shadow learning disagree")
            actions.append((received, 1, instant(event["kickoff_utc"]), identity, event))
    actions.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    # The observer creates same-cycle decisions before consuming that cycle's labels.
    causal = ShadowStrategyMemory(memory.plan)
    history = memory.payload()["records"]
    history_index = 0
    for when, kind, _, identity, event in actions:
        if kind != 0:
            continue
        while history_index < len(history) and instant(history[history_index]["ingested_at"]) < when:
            record = history[history_index]
            causal.observe(event_id=record["event_id"], decision_at=record["decision_at"],
                           result_available_at=record["result_available_at"], now=record["ingested_at"],
                           net_returns={key: Decimal(value) for key, value in record["net_returns"].items()},
                           source_sha256=record["source_sha256"], availability_basis=record["availability_basis"])
            history_index += 1
        preference = causal.choose(as_of=event["decision_at"], admissible_actions=frozenset({"PRICE_GAP", "WAIT"}))
        if preference != event["memory_preference"]:
            raise ValueError("frozen agent preference cannot be derived from prior feedback")
    book, risk = PaperBook(INITIAL), PaperRiskPolicy()
    tickets, diary = {}, []
    opened = won = lost = 0
    peak, max_drawdown = INITIAL, Decimal(0)
    for when, kind, _, identity, event in actions:
        if kind == 0:
            if event["memory_preference"] == "WAIT":
                diary.append({"id": identity, "action": "WAIT", "at": when.isoformat()})
                continue
            decision = risk.evaluate(book, STAKE)
            if not decision.allowed:
                diary.append({"id": identity, "action": "RISK_REJECTED", "reason": decision.reason, "at": when.isoformat()})
                continue
            if not _fee_reserve_allows(book, risk, opened):
                diary.append({"id": identity, "action": "RISK_REJECTED", "reason": "fee-stressed cash reserve", "at": when.isoformat()})
                continue
            leg = TicketLeg(identity, "1x2", event["selection"], Decimal(event["observed_odds"]), sport="soccer")
            ticket = book.open_ticket((leg,), STAKE, reason="simulated recorded public quote; not an accepted order",
                                      placed_at=when.isoformat())
            tickets[identity] = ticket
            opened += 1
            diary.append({"id": identity, "action": "PAPER_ASSUMED_FILL", "at": when.isoformat()})
        else:
            ticket = tickets.get(identity)
            if ticket is None:
                continue
            result_won = event["selection"] == event["result"]
            winners = {ticket.legs[0].quote_key} if result_won else set()
            book.settle(ticket.ticket_id, winners, settled_at=when.isoformat())
            won += int(result_won)
            lost += int(not result_won)
            diary.append({"id": identity, "action": "PAPER_WON" if result_won else "PAPER_LOST", "at": when.isoformat()})
        equity = book.balance + book.committed_stake - FEE * opened
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    gross_equity = book.balance + book.committed_stake
    return {"schema": "autosport.recorded_paper_projection.v1", "observer_sha256": wrapper["sha256"],
            "initial_units": str(INITIAL), "stake_units": str(STAKE), "opened_tickets": opened,
            "won": won, "lost": lost, "pending_tickets": opened-won-lost,
            "virtual_cash_excluding_cost": str(book.balance), "open_stake": str(book.committed_stake),
            "gross_realized_units": str(gross_equity - INITIAL), "cost_stress_per_opened_ticket": str(FEE),
            "cost_stressed_equity_units": str(gross_equity - FEE * opened),
            "cost_stressed_net_units": str(gross_equity - FEE * opened - INITIAL),
            "cost_stressed_max_drawdown_units": str(max_drawdown), "events": diary,
            "virtual_only": True, "assumed_fill_not_accepted_quote": True,
            "fees_in_canonical_risk": False, "fee_stressed_reserve_guard": True, "profit_earned": None,
            "real_money_execution": False, "promotion_authorized": False}
