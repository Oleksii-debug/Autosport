from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import re
import sys
from types import SimpleNamespace

from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    SettlementResolution,
)
from autosport.domain import TicketLeg
from autosport.event_lifecycle import EventPhase
from autosport.paper import PaperBook


class _Lifecycle:
    def __init__(self, record) -> None:
        self.record = record

    def records(self):
        return (self.record,)


def main(workspace: Path) -> None:
    leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
    resolution = SettlementResolution(
        event_identity="provider-a:event-1",
        settlement_ref="provider-result:1",
        quote_outcomes={leg.quote_key: "win"},
        evidence_id="provider-a-outcome-1",
        evidence_sha256="0" * 64,
        available_at="2026-09-22T07:01:00+00:00",
    )
    hostile_new_calls: list[str] = []

    def hostile_new(cls, *args, **kwargs):
        del args, kwargs
        hostile_new_calls.append("called")
        return object.__new__(cls)

    class OutcomeAuthority:
        def resolve(self, record, *, as_of: str):
            del record, as_of
            type.__setattr__(PaperBook, "__new__", staticmethod(hostile_new))
            return resolution

    coordinator = object.__new__(ContinuousSessionCoordinator)
    coordinator.paper_book_path = workspace / "paper_book.json"
    coordinator.initial_bankroll = "100"
    coordinator.outcome_authority = OutcomeAuthority()
    coordinator.lifecycle = _Lifecycle(
        SimpleNamespace(
            phase=EventPhase.COMPLETED,
            settlement_ref=resolution.settlement_ref,
            identity=resolution.event_identity,
        )
    )

    try:
        coordinator._settlement_resolutions(
            as_of="2026-09-22T07:02:00+00:00",
        )
    except ContinuousSessionError as exc:
        if re.search(
            r"PaperBook|paper book|book.*authority|dependency.*changed",
            str(exc),
            flags=re.IGNORECASE,
        ) is None:
            raise AssertionError(
                f"unexpected fail-closed reason: {exc}"
            ) from exc
    else:
        raise AssertionError(
            "PaperBook.__new__ retarget was not rejected before dispatch"
        )

    if hostile_new_calls:
        raise AssertionError(
            f"hostile PaperBook.__new__ executed: {hostile_new_calls!r}"
        )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: _paperbook_new_slot_child.py WORKSPACE")
    main(Path(sys.argv[1]))
