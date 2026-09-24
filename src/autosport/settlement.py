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

    # Freeze the exact class-level executable graph that this boundary calls
    # directly or through PaperBook.settle()/validation. Capturing only the class
    # object is insufficient because its attributes remain mutable.
    paper_dispatch_names = (
        "settle",
        "_validate_loaded_state",
        "_settlement_result",
        "_normalize_resolution_keys",
        "_validate_settled_at",
        "_validate_timestamp",
        "_validate_placed_at",
        "_require_utf8_string",
        "_require_finite",
        "_require_canonical_text",
        "_validate_ticket_provenance",
        "_validate_ticket_leg",
        "_validate_lifecycle_reachability",
        "_validate_lifecycle_entry",
        "_debit_balance",
    )

    def descriptor_function(descriptor):
        if isinstance(descriptor, (classmethod, staticmethod)):
            return descriptor.__func__
        return descriptor

    paper_dispatch_seal = tuple(
        (
            name,
            paper_book_type.__dict__[name],
            descriptor_function(paper_book_type.__dict__[name]).__code__,
        )
        for name in paper_dispatch_names
    )
    validate_book = paper_book_type._validate_loaded_state
    settlement_result = paper_book_type._settlement_result
    settle_book = paper_book_type.settle

    # Capturing a Python function freezes its code object, not the values it
    # resolves from its module globals at call time. Seal the same-module global
    # dependency graph reachable from the PaperBook methods above so a caller
    # cannot transiently rebind TicketStatus, Decimal/context helpers, or another
    # PaperBook helper without changing any class descriptor.
    paper_module_globals = descriptor_function(
        paper_book_type.__dict__["settle"]
    ).__globals__
    paper_global_seal: dict[str, tuple[object, object | None]] = {}
    pending_functions = [
        descriptor_function(descriptor)
        for _, descriptor, _ in paper_dispatch_seal
    ]
    seen_function_ids: set[int] = set()
    while pending_functions:
        function = pending_functions.pop()
        function_id = id(function)
        if function_id in seen_function_ids:
            continue
        seen_function_ids.add(function_id)
        if getattr(function, "__globals__", None) is not paper_module_globals:
            continue
        for global_name in function.__code__.co_names:
            if global_name not in paper_module_globals:
                continue
            value = paper_module_globals[global_name]
            code = getattr(value, "__code__", None)
            existing = paper_global_seal.get(global_name)
            if existing is not None and (
                existing[0] is not value or existing[1] is not code
            ):
                raise RuntimeError(
                    "PaperBook settlement global dependency is inconsistent"
                )
            paper_global_seal[global_name] = (value, code)
            if (
                code is not None
                and getattr(value, "__globals__", None) is paper_module_globals
            ):
                pending_functions.append(value)
    frozen_paper_globals = tuple(
        (name, value, code)
        for name, (value, code) in sorted(paper_global_seal.items())
    )

    # PaperBook's imported domain DTO classes are mutable class objects too.
    # Seal the exact field/property descriptors consumed by validation and
    # settlement so class-level retargeting cannot reinterpret an exact ticket.
    paper_ticket_type = paper_module_globals["PaperTicket"]
    ticket_leg_type = paper_module_globals["TicketLeg"]
    domain_dto_descriptor_names = (
        (
            paper_ticket_type,
            (
                "ticket_id",
                "stake",
                "legs",
                "placed_at",
                "status",
                "payout",
                "strategy_reason",
                "provider_source_ids",
                "provider_accounts",
                "bankroll_id",
                "currency",
                "settled_at",
            ),
        ),
        (
            ticket_leg_type,
            (
                "event_id",
                "market_id",
                "selection_id",
                "locked_odds",
                "sport",
                "exchange_side",
                "quote_key",
            ),
        ),
    )
    domain_dto_dispatch_seal = tuple(
        (
            dto_type,
            name,
            dto_type.__dict__[name],
            (
                descriptor_function(dto_type.__dict__[name]).__code__
                if hasattr(
                    descriptor_function(dto_type.__dict__[name]),
                    "__code__",
                )
                else None
            ),
        )
        for dto_type, names in domain_dto_descriptor_names
        for name in names
    )

    quote_key_descriptor = ticket_leg_type.__dict__["quote_key"]
    if not isinstance(quote_key_descriptor, property) or quote_key_descriptor.fget is None:
        raise RuntimeError("TicketLeg.quote_key must be the canonical property")
    quote_key_function = quote_key_descriptor.fget
    domain_module_globals = quote_key_function.__globals__
    domain_global_seal: dict[str, tuple[object, object | None]] = {}
    pending_domain_functions = [quote_key_function]
    seen_domain_function_ids: set[int] = set()
    while pending_domain_functions:
        function = pending_domain_functions.pop()
        function_id = id(function)
        if function_id in seen_domain_function_ids:
            continue
        seen_domain_function_ids.add(function_id)
        if getattr(function, "__globals__", None) is not domain_module_globals:
            continue
        for global_name in function.__code__.co_names:
            if global_name not in domain_module_globals:
                continue
            value = domain_module_globals[global_name]
            code = getattr(value, "__code__", None)
            existing = domain_global_seal.get(global_name)
            if existing is not None and (
                existing[0] is not value or existing[1] is not code
            ):
                raise RuntimeError(
                    "TicketLeg quote identity dependency is inconsistent"
                )
            domain_global_seal[global_name] = (value, code)
            if (
                code is not None
                and getattr(value, "__globals__", None) is domain_module_globals
            ):
                pending_domain_functions.append(value)
    frozen_domain_globals = tuple(
        (name, value, code)
        for name, (value, code) in sorted(domain_global_seal.items())
    )

    def require_domain_dto_dispatch() -> None:
        for dto_type, name, descriptor, code in domain_dto_dispatch_seal:
            current = dto_type.__dict__.get(name)
            if current is not descriptor:
                raise ValueError(
                    "settlement domain DTO authority dispatch changed"
                )
            current_function = descriptor_function(current)
            if code is not None and (
                not hasattr(current_function, "__code__")
                or current_function.__code__ is not code
            ):
                raise ValueError(
                    "settlement domain DTO authority code changed"
                )
        for name, value, code in frozen_domain_globals:
            if (
                name not in domain_module_globals
                or domain_module_globals[name] is not value
            ):
                raise ValueError(
                    "settlement quote identity authority globals changed"
                )
            if code is not None and getattr(value, "__code__", None) is not code:
                raise ValueError(
                    "settlement quote identity authority code changed"
                )

    def require_paper_module_globals() -> None:
        for name, value, code in frozen_paper_globals:
            if (
                name not in paper_module_globals
                or paper_module_globals[name] is not value
            ):
                raise ValueError(
                    "PaperBook settlement authority globals changed"
                )
            if code is not None and getattr(value, "__code__", None) is not code:
                raise ValueError(
                    "PaperBook settlement authority global code changed"
                )

    def require_paper_book_dispatch(book: PaperBook) -> None:
        require_paper_module_globals()
        require_domain_dto_dispatch()
        if type(book) is not paper_book_type:
            raise ValueError("settlement book must be an exact PaperBook")
        instance_state = vars(book)
        for name, descriptor, code in paper_dispatch_seal:
            current = paper_book_type.__dict__.get(name)
            if current is not descriptor:
                raise ValueError(
                    "PaperBook settlement authority dispatch changed"
                )
            current_function = descriptor_function(current)
            if (
                not hasattr(current_function, "__code__")
                or current_function.__code__ is not code
            ):
                raise ValueError(
                    "PaperBook settlement authority code changed"
                )
            if name in instance_state:
                raise ValueError(
                    "settlement book mutation helpers must not be shadowed"
                )

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

            require_paper_book_dispatch(book)
            validate_book(book)

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
                    settlement_result(
                        ticket,
                        simulated_balance,
                        winning,
                        voids,
                    )
                )
                plan.append((ticket.ticket_id, winning, voids))

            settled: list[str] = []
            for ticket_id, winning, voids in plan:
                # Re-check the class graph immediately before each economic
                # mutation, then invoke the captured canonical entrypoint.
                require_paper_book_dispatch(book)
                settle_book(
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
