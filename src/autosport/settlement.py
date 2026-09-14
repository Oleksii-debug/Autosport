from __future__ import annotations

from dataclasses import dataclass, field

from .domain import TicketStatus
from .paper import PaperBook


VALID_OUTCOMES = {"win", "loss", "void"}


@dataclass(slots=True)
class SettlementEngine:
    """Version-1 deterministic settlement state. Strategy code never receives this state during replay."""

    outcomes: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def _validated_outcomes_snapshot(raw: object) -> dict[str, str]:
        """Snapshot only the canonical public settlement-truth container shape."""

        # Do not coerce arbitrary mappings/iterables with dict(raw): iterable pairs
        # can contain duplicate quote keys that collapse last-wins before validation.
        # Requiring the built-in dict also prevents overridden copy/items behavior
        # from normalizing untrusted public state while this truth boundary snapshots it.
        if type(raw) is not dict:
            raise ValueError("settlement outcomes must be an exact dict")
        snapshot = raw.copy()
        for quote_key, outcome in snapshot.items():
            if (
                type(quote_key) is not str
                or not quote_key
                or quote_key.strip() != quote_key
            ):
                raise ValueError("settlement quote key must be a non-empty trimmed string")
            if type(outcome) is not str or outcome not in VALID_OUTCOMES:
                if type(outcome) is str:
                    raise ValueError(f"unsupported outcome: {outcome}")
                raise ValueError("unsupported outcome type")
        return snapshot

    def record(self, quote_outcomes: dict[str, str]) -> None:
        current = self._validated_outcomes_snapshot(self.outcomes)
        incoming = self._validated_outcomes_snapshot(quote_outcomes)
        for quote_key, outcome in incoming.items():
            previous = current.get(quote_key)
            if previous is not None and previous != outcome:
                raise ValueError(f"conflicting settlement for {quote_key}")
        self.outcomes.update(incoming)

    def settle_ready(self, book: PaperBook) -> list[str]:
        # Snapshot and revalidate public mutable settlement state. Callers can
        # construct SettlementEngine with invalid runtime values or mutate outcomes
        # directly, so record() is not the only ingress to this economic truth boundary.
        outcomes = self._validated_outcomes_snapshot(self.outcomes)

        # The commit phase must not dispatch through caller-overridable PaperBook
        # mutation behavior after a successful canonical preflight. A subclass can
        # otherwise apply an earlier ticket and fail a later settle() call, recreating
        # the partial-batch state this boundary exists to prevent.
        if type(book) is not PaperBook:
            raise ValueError("settlement book must be an exact PaperBook")

        # The commit phase below relies on stable canonical ticket identity and
        # lifecycle state. Reject caller-mutated PaperBook state before any
        # settlement mutation instead of discovering it after an earlier ticket
        # has already been applied.
        PaperBook._validate_loaded_state(book)

        # Build and economically preflight the complete ready batch before
        # mutating the PaperBook. A later ticket can fail deterministic payout
        # validation even when an earlier ticket is valid; applying tickets as
        # they are discovered would leave a partially settled book.
        plan: list[tuple[str, set[str], set[str]]] = []
        simulated_balance = book.balance

        for ticket in list(book.tickets.values()):
            if ticket.status is not TicketStatus.OPEN:
                continue
            states = [outcomes.get(leg.quote_key) for leg in ticket.legs]
            if "loss" not in states and any(state is None for state in states):
                continue
            winning = {
                leg.quote_key
                for leg in ticket.legs
                if outcomes.get(leg.quote_key) == "win"
            }
            voids = {
                leg.quote_key
                for leg in ticket.legs
                if outcomes.get(leg.quote_key) == "void"
            }
            _, _, simulated_balance = PaperBook._settlement_result(
                ticket,
                simulated_balance,
                winning,
                voids,
            )
            plan.append((ticket.ticket_id, winning, voids))

        settled: list[str] = []
        for ticket_id, winning, voids in plan:
            # Dispatch through the canonical class boundary so an exact PaperBook
            # instance cannot shadow ``settle`` in ``__dict__`` after preflight and
            # reintroduce a partial batch during apply.
            PaperBook.settle(book, ticket_id, winning, voids)
            settled.append(ticket_id)
        return settled
