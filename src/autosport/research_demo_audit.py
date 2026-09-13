from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path

from .dataset import load_dataset
from .research_strategy import RESEARCH_STRATEGY_ID, ResearchStrategyPlan
from .session import AutosportSession
from .strategies import experiment_strategy_id


_EXPECTED_EVENT_COUNT = 4
_EXPECTED_BALANCE = Decimal("990")
_EXPECTED_NET_PROFIT = Decimal("-10")
_EXPECTED_TICKET_COUNT = 1


def run_research_demo_audit(
    output_path: str | Path,
    demo_root: str | Path,
) -> int:
    """Execute the shipped typed research demo through the frozen product path.

    This is deterministic sample reachability evidence only. It deliberately does
    not promote the fixture to real historical-market, profitability, human, or
    NVDA evidence.
    """

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        demo = Path(demo_root)
        dataset = load_dataset(demo)
        plan = ResearchStrategyPlan.from_path(demo / "research_plan.json")
        plan.preflight(dataset.load_market_events())
        strategy_identity = experiment_strategy_id(RESEARCH_STRATEGY_ID, plan)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "research-demo-workspace"
            session = AutosportSession(
                workspace,
                "1000",
                strategy_id=RESEARCH_STRATEGY_ID,
                research_plan=plan,
            )
            try:
                result = session.run_dataset(dataset)
                ticket_ids = tuple(sorted(session.book.tickets))
            finally:
                session.close()

            if result.replay.event_count != _EXPECTED_EVENT_COUNT:
                raise RuntimeError(
                    f"research demo replay event count drifted: {result.replay.event_count}"
                )
            if len(result.settled_ticket_ids) != _EXPECTED_TICKET_COUNT:
                raise RuntimeError(
                    "research demo did not settle exactly one paper ticket"
                )
            if len(ticket_ids) != _EXPECTED_TICKET_COUNT:
                raise RuntimeError(
                    "research demo PaperBook did not persist exactly one ticket"
                )
            if result.balance != _EXPECTED_BALANCE:
                raise RuntimeError(
                    f"research demo balance drifted: {result.balance}"
                )
            if result.evaluation.net_profit != _EXPECTED_NET_PROFIT:
                raise RuntimeError(
                    f"research demo paper net profit drifted: {result.evaluation.net_profit}"
                )

            reopened = AutosportSession(
                workspace,
                "1",
                strategy_id=RESEARCH_STRATEGY_ID,
                research_plan=plan,
            )
            try:
                if reopened.book.balance != result.balance:
                    raise RuntimeError(
                        "research demo PaperBook balance changed across restart"
                    )
                if tuple(sorted(reopened.book.tickets)) != ticket_ids:
                    raise RuntimeError(
                        "research demo ticket identity changed across restart"
                    )
                if reopened.registry.in_progress():
                    raise RuntimeError(
                        "research demo completed run became unresolved after restart"
                    )
            finally:
                reopened.close()

        payload = {
            "status": "PASS",
            "strategy_id": RESEARCH_STRATEGY_ID,
            "strategy_identity": strategy_identity,
            "research_plan_sha256": plan.source_sha256,
            "dataset_market_sha256": dataset.market_sha256,
            "dataset_results_sha256": dataset.results_sha256,
            "replay_event_count": result.replay.event_count,
            "settled_ticket_count": len(result.settled_ticket_ids),
            "persistent_ticket_count": len(ticket_ids),
            "persistent_balance": str(result.balance),
            "paper_net_profit": str(result.evaluation.net_profit),
            "sample_fixture": True,
            "real_historical_market_proof": False,
            "profitability_claim": False,
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "error": f"{type(exc).__name__}: {exc}",
            "sample_fixture": True,
            "real_historical_market_proof": False,
            "profitability_claim": False,
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 1
