from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock

from .domain import TicketStatus
from .paper import PaperBook


VALID_OUTCOMES = {"win", "loss", "void"}
@dataclass(slots=True)
class SettlementEngine:
    """Version-1 deterministic settlement state. Strategy code never receives this state during replay."""

    outcomes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Own the constructor handoff. Retaining a caller-owned exact dict would
        # let that caller rewrite terminal settlement truth later without going
        # through record() or the serialization boundary.
        #
        # Preserve the existing delayed validation contract for malformed
        # non-dict state: settle_ready()/record() remain the fail-closed ingress.
        if type(self.outcomes) is dict:
            self.outcomes = self.outcomes.copy()

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
            if type(outcome) is not str or outcome not in ("win", "loss", "void"):
                if type(outcome) is str:
                    raise ValueError(f"unsupported outcome: {outcome}")
                raise ValueError("unsupported outcome type")
        return snapshot


def _build_serialized_settlement_operations():
    """Build one non-retargetable in-process settlement serialization domain."""

    serialization_lock = Lock()
    engine_type = SettlementEngine
    validate_outcomes = engine_type._validated_outcomes_snapshot
    paper_book_type = PaperBook
    open_status = TicketStatus.OPEN

    def record(self, quote_outcomes: dict[str, str]) -> None:
        # Conflict detection and publication are one serialized transition.
        # The lock is closure-owned so a module-global rebind cannot split
        # concurrent official operations into different serialization domains.
        with serialization_lock:
            current = validate_outcomes(self.outcomes)
            incoming = validate_outcomes(quote_outcomes)
            for quote_key, outcome in incoming.items():
                previous = current.get(quote_key)
                if previous is not None and previous != outcome:
                    raise ValueError(
                        f"conflicting settlement for {quote_key}"
                    )
            self.outcomes.update(incoming)

    def settle_ready(self, book: PaperBook) -> list[str]:
        # Keep one exact lock from outcome snapshot through the entire canonical
        # PaperBook economic commit. record() consumes this same closure-owned
        # lock, so neither operation can be retargeted at runtime.
        with serialization_lock:
            outcomes = validate_outcomes(self.outcomes)

            if type(book) is not paper_book_type:
                raise ValueError(
                    "settlement book must be an exact PaperBook"
                )

            if any(
                helper in vars(book)
                for helper in (
                    "_normalize_resolution_keys",
                    "_settlement_result",
                )
            ):
                raise ValueError(
                    "settlement book mutation helpers must not be shadowed"
                )

            paper_book_type._validate_loaded_state(book)

            plan: list[tuple[str, set[str], set[str]]] = []
            simulated_balance = book.balance

            for ticket in list(book.tickets.values()):
                if ticket.status is not open_status:
                    continue
                states = [
                    outcomes.get(leg.quote_key)
                    for leg in ticket.legs
                ]
                if (
                    "loss" not in states
                    and any(state is None for state in states)
                ):
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
                _, _, simulated_balance = (
                    paper_book_type._settlement_result(
                        ticket,
                        simulated_balance,
                        winning,
                        voids,
                    )
                )
                plan.append((ticket.ticket_id, winning, voids))

            settled: list[str] = []
            for ticket_id, winning, voids in plan:
                paper_book_type.settle(
                    book,
                    ticket_id,
                    winning,
                    voids,
                )
                settled.append(ticket_id)
            return settled

    record.__name__ = "record"
    record.__qualname__ = "SettlementEngine.record"
    settle_ready.__name__ = "settle_ready"
    settle_ready.__qualname__ = "SettlementEngine.settle_ready"
    return record, settle_ready


SettlementEngine.record, SettlementEngine.settle_ready = (
    _build_serialized_settlement_operations()
)
del _build_serialized_settlement_operations
