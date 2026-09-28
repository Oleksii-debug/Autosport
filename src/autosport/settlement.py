from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from weakref import ReferenceType, ref

from .domain import TicketStatus
from .paper import PaperBook


VALID_OUTCOMES = {"win", "loss", "void"}

# Descriptive only: this contract is not consulted to grant settlement authority.
# The canonical guards below operate inside one trusted CPython process. They fail
# closed on supported ordinary application-level state, binding, descriptor, and
# dependency drift. They do not claim to survive direct reflective mutation of the
# guarding class/metaclass graph through builtin type operations, arbitrary mutation
# of the executable objects, or runtime mutation that implements the guards
# themselves. A process with that capability is inside the trusted computing base
# and must be treated as compromised.
SETTLEMENT_TAMPER_MODEL_VERSION = "settlement-process-trust-v1"
SETTLEMENT_TAMPER_MODEL_V1 = (
    "assumes:trusted-cpython-runtime",
    "assumes:trusted-installed-python-executables",
    "guards:ordinary-application-state-binding-descriptor-dependency-drift",
    "out-of-scope:direct-builtin-type-class-metaclass-mutation",
    "out-of-scope:direct-function-code-mutation",
    "out-of-scope:closure-cell-mutation",
    "out-of-scope:interpreter-or-native-runtime-mutation",
)





def _build_public_entry_class_guard(name: str):
    """Guard a public class binding inside the declared trusted-process model."""

    class PublicEntryClassGuard:
        __slots__ = ()

        def __get__(self, instance, owner=None):
            if instance is None:
                return self
            return instance.__dict__[name]

        def __set__(self, _instance, _value) -> None:
            raise TypeError(
                "canonical settlement public entry binding is immutable"
            )

        def __delete__(self, _instance) -> None:
            raise TypeError(
                "canonical settlement public entry binding is immutable"
            )

    return PublicEntryClassGuard()


class _SettlementEngineMeta(type):
    """Seal public entry bindings against supported ordinary application retargeting."""

    def __setattr__(cls, name: str, value: object) -> None:
        if (
            cls.__dict__.get("_public_entry_bindings_sealed", False)
            and name
            in {
                "__post_init__",
                "record",
                "settle_ready",
                "_public_entry_bindings_sealed",
            }
        ):
            raise TypeError(
                "canonical settlement public entry binding is immutable"
            )
        super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        if (
            cls.__dict__.get("_public_entry_bindings_sealed", False)
            and name
            in {
                "__post_init__",
                "record",
                "settle_ready",
                "_public_entry_bindings_sealed",
            }
        ):
            raise TypeError(
                "canonical settlement public entry binding is immutable"
            )
        super().__delattr__(name)


@dataclass(slots=True, weakref_slot=True)
class SettlementEngine(metaclass=_SettlementEngineMeta):
    """Version-1 deterministic settlement state.

    Strategy code never receives this state during replay. Tamper-resistance claims
    are scoped by SETTLEMENT_TAMPER_MODEL_V1; arbitrary same-process reflective
    class/metaclass, executable, or interpreter mutation is not represented as an
    independently protected root.
    """

    _public_entry_bindings_sealed = False
    outcomes: dict[str, str] = field(default_factory=dict)
    _outcomes_authority: object | None = field(
        init=False,
        repr=False,
        compare=False,
        default=None,
    )

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
    raw_post_init = engine_type.__post_init__
    validate_outcomes = engine_type._validated_outcomes_snapshot
    outcomes_descriptor = engine_type.__dict__["outcomes"]
    outcomes_authority_descriptor = engine_type.__dict__["_outcomes_authority"]
    paper_book_type = PaperBook
    open_status = TicketStatus.OPEN

    class OutcomeAuthorityToken:
        __slots__ = ("__weakref__",)

    authority_by_engine_id: dict[
        int,
        tuple[
            ReferenceType[SettlementEngine],
            OutcomeAuthorityToken | None,
            tuple[tuple[object, object], ...] | None,
        ],
    ] = {}

    def _new_owner_ref(engine: SettlementEngine) -> ReferenceType[SettlementEngine]:
        key = id(engine)

        def forget(owner_ref: ReferenceType[SettlementEngine]) -> None:
            current = authority_by_engine_id.get(key)
            if current is not None and current[0] is owner_ref:
                authority_by_engine_id.pop(key, None)

        return ref(engine, forget)

    def issue_outcomes_authority(
        engine: SettlementEngine,
        snapshot: dict[str, str],
    ) -> None:
        key = id(engine)
        current = authority_by_engine_id.get(key)
        if current is None or current[0]() is not engine:
            owner_ref = _new_owner_ref(engine)
        else:
            owner_ref = current[0]
        token = OutcomeAuthorityToken()
        authority_by_engine_id[key] = (
            owner_ref,
            token,
            tuple(snapshot.items()),
        )
        outcomes_authority_descriptor.__set__(engine, token)

    def guarded_post_init(self: SettlementEngine) -> None:
        with serialization_lock:
            key = id(self)
            current = authority_by_engine_id.get(key)
            if current is not None and current[0]() is self:
                raise ValueError("settlement outcome authority already initialized")

            raw_post_init(self)
            raw = outcomes_descriptor.__get__(self, engine_type)
            if type(raw) is dict:
                issue_outcomes_authority(self, raw)
            else:
                owner_ref = _new_owner_ref(self)
                authority_by_engine_id[key] = (owner_ref, None, None)

    def require_outcomes_authority(
        engine: SettlementEngine,
        snapshot: dict[str, str] | None = None,
    ) -> object:
        raw = outcomes_descriptor.__get__(engine, engine_type)
        authorized = outcomes_authority_descriptor.__get__(engine, engine_type)
        binding = authority_by_engine_id.get(id(engine))
        if binding is None or binding[0]() is not engine:
            raise ValueError("settlement outcome authority changed")
        if authorized is None:
            # Preserve delayed validation for a malformed non-dict constructor
            # value, but never allow caller replacement of that state with a
            # valid-looking dict or a replayed __post_init__ to create authority.
            if binding[1] is not None or binding[2] is not None or type(raw) is dict:
                raise ValueError("settlement outcome authority changed")
            return raw
        if (
            type(authorized) is not OutcomeAuthorityToken
            or type(raw) is not dict
            or authorized is not binding[1]
            or binding[2] is None
        ):
            raise ValueError("settlement outcome authority changed")
        expected = dict(binding[2])
        if raw != expected or (snapshot is not None and snapshot != expected):
            raise ValueError("settlement outcome authority changed")
        return raw

    # Capture the exact class-level executable graph that this boundary calls
    # directly or through PaperBook.settle()/validation. Within the declared
    # trusted-process model, descriptor/code checks are defense in depth against
    # application-level drift. They are not a claim that Python can self-attest
    # against arbitrary mutation of the currently trusted executable objects.
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
        if isinstance(descriptor, property):
            return descriptor.fget
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

    # Function identity alone does not bind the module globals resolved at call
    # time. Capture the same-module global dependency graph reachable from the
    # PaperBook methods above so supported application-level rebinding of
    # TicketStatus, Decimal/context helpers, or another PaperBook helper fails
    # closed. Exact code-object checks here are defense in depth within the same
    # trusted-process boundary, not an external executable-integrity root.
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
            raw = require_outcomes_authority(self)
            current = validate_outcomes(raw)
            require_outcomes_authority(self, current)
            incoming = validate_outcomes(quote_outcomes)
            for quote_key, outcome in incoming.items():
                previous = current.get(quote_key)
                if previous is not None and previous != outcome:
                    raise ValueError(
                        f"conflicting settlement for {quote_key}"
                    )
            published = current.copy()
            published.update(incoming)
            outcomes_descriptor.__set__(self, published)
            issue_outcomes_authority(self, published)

    def settle_ready(self, book: PaperBook) -> list[str]:
        # Keep one exact lock from outcome snapshot through the entire canonical
        # PaperBook economic commit. record() consumes this same closure-owned
        # lock, so neither operation can be retargeted at runtime.
        with serialization_lock:
            raw = require_outcomes_authority(self)
            outcomes = validate_outcomes(raw)
            require_outcomes_authority(self, outcomes)

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

    guarded_post_init.__name__ = "__post_init__"
    guarded_post_init.__qualname__ = "SettlementEngine.__post_init__"
    record.__name__ = "record"
    record.__qualname__ = "SettlementEngine.record"
    settle_ready.__name__ = "settle_ready"
    settle_ready.__qualname__ = "SettlementEngine.settle_ready"
    return guarded_post_init, record, settle_ready


(
    SettlementEngine.__post_init__,
    SettlementEngine.record,
    SettlementEngine.settle_ready,
) = _build_serialized_settlement_operations()
# A metaclass data descriptor is intentionally installed only after the canonical
# functions exist in SettlementEngine.__dict__.  Unlike an __setattr__ override
# alone, type.__setattr__/type.__delattr__ still honor data descriptors on the
# metaclass, so direct base-metaclass mutation cannot replace these public gates.
_SettlementEngineMeta.__post_init__ = _build_public_entry_class_guard("__post_init__")
_SettlementEngineMeta.record = _build_public_entry_class_guard("record")
_SettlementEngineMeta.settle_ready = _build_public_entry_class_guard("settle_ready")
SettlementEngine._public_entry_bindings_sealed = True
del _build_public_entry_class_guard
del _build_serialized_settlement_operations
