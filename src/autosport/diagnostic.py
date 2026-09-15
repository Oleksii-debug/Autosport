from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from .domain import MarketEvent, TicketLeg
from .integrity import atomic_write_json
from .paper import PaperBook
from .replay import ReplayEngine, ReplayLeakageFirewall
from .storage import SQLiteMarketStore


def _render_exception(exc: BaseException) -> str:
    exception_type = type(exc).__name__
    try:
        details = str(exc)
    except BaseException:
        return f"{exception_type}: exception details unavailable"
    return f"{exception_type}: {details}"


def run_machine_diagnostic(output_path: str | Path) -> int:
    destination = Path(output_path)
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
                        f"{_render_exception(cleanup_error)}"
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
        atomic_write_json(destination, payload)
        return 0
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "error": _render_exception(exc),
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        error_notes = getattr(exc, "__notes__", None)
        if error_notes:
            payload["error_notes"] = list(error_notes)
        atomic_write_json(destination, payload)
        return 1
