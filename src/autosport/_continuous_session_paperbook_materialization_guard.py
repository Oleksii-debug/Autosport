"""Seal domain-object materialization used by the canonical PaperBook loader.

This is a composition guard around the already-installed ``_load_book`` helper.  It
owns no book, settlement, provider, or persistence authority; it only proves that the
existing PaperBook parser still materializes the exact canonical domain DTOs and
runtime slots that later source-scope/economic checks trust.
"""

from __future__ import annotations

import dis as _dis
from typing import Any

from . import continuous_session as _session
from . import paper as _paper


def _install() -> None:
    session_module = _session
    paper_module = _paper
    coordinator = session_module.ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator, "__dict__")
    original_load_book = coordinator_dict["_load_book"]
    original_settlement_resolutions = coordinator_dict["_settlement_resolutions"]

    exact_type = type
    exact_len = len
    exact_getattr = getattr
    exact_zip = zip
    exact_value_error = ValueError
    missing = object()
    paper_namespace = exact_type(paper_module).__getattribute__(paper_module, "__dict__")

    canonical_paper_book = paper_namespace.get("PaperBook", missing)
    canonical_paper_ticket = paper_namespace.get("PaperTicket", missing)
    canonical_ticket_leg = paper_namespace.get("TicketLeg", missing)
    canonical_ticket_status = paper_namespace.get("TicketStatus", missing)
    canonical_decimal = paper_namespace.get("Decimal", missing)
    canonical_decimal_exception = paper_namespace.get("DecimalException", missing)
    canonical_parse_iso_timestamp = paper_namespace.get("parse_iso_timestamp", missing)
    if missing in {
        canonical_paper_book,
        canonical_paper_ticket,
        canonical_ticket_leg,
        canonical_ticket_status,
        canonical_decimal,
        canonical_decimal_exception,
        canonical_parse_iso_timestamp,
    }:
        raise TypeError("canonical PaperBook materialization dependency is unavailable")

    # Positive snapshot materialization replays ticket chronology/economics before the
    # loaded book is admitted. Those methods late-resolve their Decimal context and
    # parser/schema helpers from this same mutable module globals mapping. Witness the
    # exact direct dependency graph here, in the one materialization owner, so a
    # callback cannot execute a hostile self-restoring arithmetic helper underneath an
    # otherwise unchanged PaperBook method/class surface.
    replay_dependency_names = (
        "_paper_decimal_context",
        "Context",
        "Inexact",
        "InvalidOperation",
        "Overflow",
        "ROUND_HALF_EVEN",
        "Underflow",
        "localcontext",
        "_PAPER_DECIMAL_PRECISION",
        "_PAPER_DECIMAL_EMIN",
        "_PAPER_DECIMAL_EMAX",
        "_SCHEMA_MISSING",
        "_SUPPORTED_PAPER_SNAPSHOT_SCHEMA_VERSIONS",
    )
    replay_dependency_witnesses = tuple(
        (name, paper_namespace.get(name, missing)) for name in replay_dependency_names
    )
    if any(expected is missing for _name, expected in replay_dependency_witnesses):
        raise TypeError("canonical PaperBook replay dependency is unavailable")
    replay_executable_witnesses = tuple(
        (name, expected, code)
        for name, expected in replay_dependency_witnesses
        if (code := exact_getattr(expected, "__code__", None)) is not None
    )

    # Keep every global expected value in one long-lived closure cell. The final Wave M
    # dispatch seal witnesses the outer wrappers that use this graph, while the wrappers
    # also pin the nested preflight closure cells themselves across callback intervals.
    frozen_materialization_global_witnesses = (
        ("PaperTicket", canonical_paper_ticket),
        ("TicketLeg", canonical_ticket_leg),
        ("TicketStatus", canonical_ticket_status),
        ("Decimal", canonical_decimal),
        ("DecimalException", canonical_decimal_exception),
        ("parse_iso_timestamp", canonical_parse_iso_timestamp),
        *replay_dependency_witnesses,
    )

    paper_book_dict = exact_type.__getattribute__(canonical_paper_book, "__dict__")
    ticket_dict = exact_type.__getattribute__(canonical_paper_ticket, "__dict__")
    leg_dict = exact_type.__getattribute__(canonical_ticket_leg, "__dict__")
    status_dict = exact_type.__getattribute__(canonical_ticket_status, "__dict__")

    canonical_ticket_init = ticket_dict.get("__init__", missing)
    canonical_leg_init = leg_dict.get("__init__", missing)
    canonical_leg_post_init = leg_dict.get("__post_init__", missing)
    canonical_leg_quote_key = leg_dict.get("quote_key", missing)
    function_type = exact_type(original_load_book)
    for function in (canonical_ticket_init, canonical_leg_init, canonical_leg_post_init):
        if exact_type(function) is not function_type:
            raise TypeError("canonical PaperBook materializer executable is unavailable")
    if exact_type(canonical_leg_quote_key) is not property:
        raise TypeError("canonical TicketLeg quote_key property is unavailable")
    canonical_leg_quote_key_getter = canonical_leg_quote_key.fget
    if exact_type(canonical_leg_quote_key_getter) is not function_type:
        raise TypeError("canonical TicketLeg quote_key executable is unavailable")

    # TicketStatus(value) does not dispatch only through the exact enum class object:
    # EnumType.__call__ and the class's value->member mapping decide which canonical
    # member is returned. Seal both so an outcome callback cannot preserve the exact
    # TicketStatus object while relabelling durable OPEN/LOST/WON/VOID economics.
    status_metaclass = exact_type(canonical_ticket_status)
    status_metaclass_dict = exact_type.__getattribute__(status_metaclass, "__dict__")
    canonical_status_call = status_metaclass_dict.get("__call__", missing)
    if exact_type(canonical_status_call) is not function_type:
        raise TypeError("canonical TicketStatus conversion executable is unavailable")
    canonical_status_call_code = canonical_status_call.__code__
    canonical_status_value_map = status_dict.get("_value2member_map_", missing)
    if exact_type(canonical_status_value_map) is not dict:
        raise TypeError("canonical TicketStatus value/member mapping is unavailable")
    canonical_status_relations = tuple(
        (object.__getattribute__(member, "_value_"), name, member)
        for name, member in canonical_ticket_status.__members__.items()
    )
    if not canonical_status_relations:
        raise TypeError("canonical TicketStatus members are unavailable")
    for value, name, member in canonical_status_relations:
        if (
            status_dict.get(name, missing) is not member
            or canonical_status_value_map.get(value, missing) is not member
        ):
            raise TypeError("canonical TicketStatus value/member relation is malformed")

    executable_witnesses = (
        (canonical_ticket_init, canonical_ticket_init.__code__),
        (canonical_leg_init, canonical_leg_init.__code__),
        (canonical_leg_post_init, canonical_leg_post_init.__code__),
        (canonical_leg_quote_key_getter, canonical_leg_quote_key_getter.__code__),
        (canonical_parse_iso_timestamp, canonical_parse_iso_timestamp.__code__),
    )

    # PaperBook's canonical parser/validators also execute process-global Python
    # builtins (for example type/tuple/list/dict/len/set/sorted/isinstance/str). Those
    # bindings are not module globals and are therefore invisible to the materializer
    # binding/code witnesses above. Derive the exact builtin authority from real
    # LOAD_GLOBAL instructions instead of growing a one-name denylist. The witness set
    # covers the full canonical PaperBook Python callable surface plus the already
    # selected materializer/replay/status executables reached during positive load.
    def python_callable(slot: object) -> object | None:
        if exact_type(slot) in {classmethod, staticmethod}:
            target = slot.__func__
        elif exact_type(slot) is property:
            target = slot.fget
        elif exact_type(slot) is function_type:
            target = slot
        else:
            return None
        if exact_type(target) is not function_type:
            return None
        return target

    builtin_targets: list[object] = []

    def add_builtin_target(target: object) -> None:
        if exact_type(target) is not function_type:
            return
        for existing in builtin_targets:
            if existing is target:
                return
        builtin_targets.append(target)

    for slot in paper_book_dict.values():
        target = python_callable(slot)
        if target is not None:
            add_builtin_target(target)
    for function, _expected_code in executable_witnesses:
        add_builtin_target(function)
    for _name, function, _expected_code in replay_executable_witnesses:
        add_builtin_target(function)
    add_builtin_target(canonical_status_call)

    actual_builtin_witnesses: list[tuple[object, str, object, object, object]] = []
    for target in builtin_targets:
        builtin_mapping = exact_getattr(target, "__builtins__", None)
        target_globals = exact_getattr(target, "__globals__", None)
        if exact_type(builtin_mapping) is not dict or exact_type(target_globals) is not dict:
            raise TypeError("canonical PaperBook builtin authority mapping is unavailable")
        witnessed_names: set[str] = set()
        for instruction in _dis.get_instructions(target):
            if instruction.opname != "LOAD_GLOBAL":
                continue
            name = instruction.argval
            if exact_type(name) is not str or name in witnessed_names or name in target_globals:
                continue
            if name not in builtin_mapping:
                continue
            witnessed_names.add(name)
            actual_builtin_witnesses.append(
                (target, name, target_globals, builtin_mapping, builtin_mapping[name])
            )
    frozen_actual_builtin_witnesses = tuple(actual_builtin_witnesses)

    # These exact descriptors are read by the post-load source/economic validators.
    # Replacing one can change the meaning of an otherwise exact dataclass instance.
    ticket_slot_witnesses = tuple(
        (name, ticket_dict.get(name, missing))
        for name in (
            "ticket_id",
            "stake",
            "legs",
            "placed_at",
            "status",
            "payout",
            "provider_source_ids",
            "provider_accounts",
            "bankroll_id",
            "currency",
            "settled_at",
        )
    )
    leg_slot_witnesses = tuple(
        (name, leg_dict.get(name, missing))
        for name in (
            "event_id",
            "market_id",
            "selection_id",
            "locked_odds",
            "sport",
            "exchange_side",
            "quote_key",
        )
    )
    if any(slot is missing for _, slot in (*ticket_slot_witnesses, *leg_slot_witnesses)):
        raise TypeError("canonical PaperBook domain slot is unavailable")

    def require_materialization_authority() -> None:
        for name, expected in frozen_materialization_global_witnesses:
            if paper_namespace.get(name, missing) is not expected:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook materialization dependency changed: " + name
                )
        for name, expected, expected_code in replay_executable_witnesses:
            if exact_getattr(expected, "__code__", None) is not expected_code:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook materialization executable changed: " + name
                )
        for (
            target,
            name,
            target_globals,
            builtin_mapping,
            expected,
        ) in frozen_actual_builtin_witnesses:
            if (
                exact_getattr(target, "__globals__", None) is not target_globals
                or target_globals.get(name, missing) is not missing
                or exact_getattr(target, "__builtins__", None) is not builtin_mapping
                or builtin_mapping.get(name, missing) is not expected
            ):
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook builtin authority changed: " + name
                )

        if ticket_dict.get("__init__", missing) is not canonical_ticket_init:
            raise session_module.ContinuousSessionError(
                "canonical PaperTicket constructor authority changed"
            )
        if leg_dict.get("__init__", missing) is not canonical_leg_init:
            raise session_module.ContinuousSessionError(
                "canonical TicketLeg constructor authority changed"
            )
        if leg_dict.get("__post_init__", missing) is not canonical_leg_post_init:
            raise session_module.ContinuousSessionError(
                "canonical TicketLeg post-init authority changed"
            )
        if leg_dict.get("quote_key", missing) is not canonical_leg_quote_key:
            raise session_module.ContinuousSessionError(
                "canonical TicketLeg quote-key authority changed"
            )
        for function, expected_code in executable_witnesses:
            if function.__code__ is not expected_code:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook materialization executable changed"
                )
        for name, expected in ticket_slot_witnesses:
            if ticket_dict.get(name, missing) is not expected:
                raise session_module.ContinuousSessionError(
                    "canonical PaperTicket field dispatch changed: " + name
                )
        for name, expected in leg_slot_witnesses:
            if leg_dict.get(name, missing) is not expected:
                raise session_module.ContinuousSessionError(
                    "canonical TicketLeg field dispatch changed: " + name
                )

        if status_metaclass_dict.get("__call__", missing) is not canonical_status_call:
            raise session_module.ContinuousSessionError(
                "canonical TicketStatus conversion dispatch changed"
            )
        if canonical_status_call.__code__ is not canonical_status_call_code:
            raise session_module.ContinuousSessionError(
                "canonical TicketStatus conversion executable changed"
            )
        if status_dict.get("_value2member_map_", missing) is not canonical_status_value_map:
            raise session_module.ContinuousSessionError(
                "canonical TicketStatus value/member authority changed"
            )
        if exact_len(canonical_status_value_map) != exact_len(canonical_status_relations):
            raise session_module.ContinuousSessionError(
                "canonical TicketStatus value/member authority changed"
            )
        current_members = canonical_ticket_status.__members__
        if exact_len(current_members) != exact_len(canonical_status_relations):
            raise session_module.ContinuousSessionError(
                "canonical TicketStatus member authority changed"
            )
        for value, name, member in canonical_status_relations:
            if (
                status_dict.get(name, missing) is not member
                or canonical_status_value_map.get(value, missing) is not member
                or current_members.get(name, missing) is not member
                or object.__getattribute__(member, "_value_") != value
            ):
                raise session_module.ContinuousSessionError(
                    "canonical TicketStatus value/member authority changed"
                )

    materialization_preflight = require_materialization_authority
    materialization_preflight_code = materialization_preflight.__code__
    materialization_preflight_closure = materialization_preflight.__closure__ or ()
    if exact_len(materialization_preflight_closure) != exact_len(
        materialization_preflight_code.co_freevars
    ):
        raise TypeError("canonical PaperBook materialization preflight closure is malformed")
    materialization_preflight_cells = tuple(
        (cell, cell.cell_contents) for cell in materialization_preflight_closure
    )

    def guarded_load_book(self: Any):
        if exact_getattr(materialization_preflight, "__code__", None) is not materialization_preflight_code:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook materialization preflight changed"
            )
        current_closure = exact_getattr(materialization_preflight, "__closure__", None) or ()
        if exact_len(current_closure) != exact_len(materialization_preflight_cells):
            raise session_module.ContinuousSessionError(
                "canonical PaperBook materialization preflight changed"
            )
        for current_cell, (expected_cell, expected_value) in exact_zip(
            current_closure,
            materialization_preflight_cells,
        ):
            if current_cell is not expected_cell:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook materialization preflight changed"
                )
            try:
                current_value = current_cell.cell_contents
            except exact_value_error as exc:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook materialization preflight changed"
                ) from exc
            if current_value is not expected_value:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook materialization preflight changed"
                )
        materialization_preflight()
        book = original_load_book(self)
        materialization_preflight()
        current_closure = exact_getattr(materialization_preflight, "__closure__", None) or ()
        if exact_len(current_closure) != exact_len(materialization_preflight_cells):
            raise session_module.ContinuousSessionError(
                "canonical PaperBook materialization preflight changed"
            )
        for current_cell, (expected_cell, expected_value) in exact_zip(
            current_closure,
            materialization_preflight_cells,
        ):
            if current_cell is not expected_cell or current_cell.cell_contents is not expected_value:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook materialization preflight changed"
                )
        return book

    guarded_load_book.__name__ = original_load_book.__name__
    guarded_load_book.__qualname__ = original_load_book.__qualname__
    guarded_load_book.__doc__ = original_load_book.__doc__
    guarded_load_book.__annotations__ = original_load_book.__annotations__

    def guarded_settlement_resolutions(self: Any, *args: Any, **kwargs: Any):
        # This method is itself sealed by the later Wave M dispatch guard. Because the
        # immutable preflight witness tuple is a direct closure dependency here, an
        # earlier collector/desktop/lifecycle callback cannot pair-rebind nested
        # materialization expectations before outcome resolution begins.
        if exact_getattr(materialization_preflight, "__code__", None) is not materialization_preflight_code:
            raise session_module.ContinuousSessionError(
                "canonical PaperBook materialization preflight changed"
            )
        current_closure = exact_getattr(materialization_preflight, "__closure__", None) or ()
        if exact_len(current_closure) != exact_len(materialization_preflight_cells):
            raise session_module.ContinuousSessionError(
                "canonical PaperBook materialization preflight changed"
            )
        for current_cell, (expected_cell, expected_value) in exact_zip(
            current_closure,
            materialization_preflight_cells,
        ):
            if current_cell is not expected_cell:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook materialization preflight changed"
                )
            try:
                current_value = current_cell.cell_contents
            except exact_value_error as exc:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook materialization preflight changed"
                ) from exc
            if current_value is not expected_value:
                raise session_module.ContinuousSessionError(
                    "canonical PaperBook materialization preflight changed"
                )
        materialization_preflight()
        resolutions = original_settlement_resolutions(self, *args, **kwargs)
        materialization_preflight()
        return resolutions

    guarded_settlement_resolutions.__name__ = original_settlement_resolutions.__name__
    guarded_settlement_resolutions.__qualname__ = original_settlement_resolutions.__qualname__
    guarded_settlement_resolutions.__doc__ = original_settlement_resolutions.__doc__
    guarded_settlement_resolutions.__annotations__ = original_settlement_resolutions.__annotations__

    type.__setattr__(coordinator, "_load_book", guarded_load_book)
    type.__setattr__(coordinator, "_settlement_resolutions", guarded_settlement_resolutions)


_install()
del _install
