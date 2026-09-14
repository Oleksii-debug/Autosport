from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path

from .domain import MarketEvent, TicketLeg
from .paper import PaperBook
from .replay import ReplayEngine, ReplayLeakageFirewall
from .storage import SQLiteMarketStore


def run_machine_diagnostic(output_path: str | Path) -> int:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        event = MarketEvent.from_dict({
            "event_id": "diag-event",
            "market_id": "winner",
            "selection_id": "a",
            "decimal_odds": "2.0",
            "observed_ts": "2026-01-01T00:00:00+00:00",
            "source_id": "diagnostic",
            "sequence": 1,
        })
        firewall = ReplayLeakageFirewall({event.event_id: event.selection_id})
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "diag.db")
            try:
                ReplayEngine([event], firewall).run(store.append, run_id="packaged-diagnostic")
                if firewall.result_for(event.event_id) != event.selection_id:
                    raise RuntimeError("replay firewall/result diagnostic failed")
                if store.current()[event.quote_key].decimal_odds != Decimal("2.0"):
                    raise RuntimeError("market storage diagnostic failed")
                book = PaperBook("100")
                leg = TicketLeg(event.event_id, event.market_id, event.selection_id, Decimal("2.0"))
                ticket = book.open_ticket([leg], "10", reason="packaged diagnostic")
                book.settle(ticket.ticket_id, {leg.quote_key})
                if book.balance != Decimal("110.0"):
                    raise RuntimeError("paper settlement diagnostic failed")
            except BaseException as primary_error:
                try:
                    store.close()
                except BaseException as cleanup_error:
                    primary_error.add_note(
                        "SQLiteMarketStore.close() also failed while preserving the primary diagnostic failure: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
                raise
            else:
                store.close()
        payload = {
            "status": "PASS",
            "virtual_balance": "110.0",
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "error": f"{type(exc).__name__}: {exc}",
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 1
