from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from decimal import (
    Context,
    Decimal,
    DecimalException,
    Inexact,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    Underflow,
    localcontext,
)
from functools import wraps
from pathlib import Path
from types import FunctionType
from weakref import WeakKeyDictionary, ref

from .domain import PaperTicket, TicketLeg, TicketStatus, utc_now_iso
from .forecasting import parse_iso_timestamp


_PAPER_DECIMAL_PRECISION = 28
_PAPER_DECIMAL_EMIN = -999999
_PAPER_DECIMAL_EMAX = 999999
_MAX_PAPER_DECIMAL_TEXT_CHARS = 512
_PAPER_SNAPSHOT_SCHEMA_VERSION = 7
_SUPPORTED_PAPER_SNAPSHOT_SCHEMA_VERSIONS = frozenset({2, 3, 4, 5, 6, 7})
_SCHEMA_MISSING = object()

_LifecycleEntry = tuple[str, str, tuple[str, ...], tuple[str, ...]]


def _make_paperbook_type_authority():
    canonical_type = None

    def install(candidate: type) -> None:
        nonlocal canonical_type
        if canonical_type is not None:
            raise RuntimeError("PaperBook canonical type authority already installed")
        if type(candidate) is not type:
            raise TypeError("PaperBook canonical type authority must be an exact class")
        canonical_type = candidate

    def require(target: object) -> None:
        if canonical_type is None:
            raise RuntimeError("PaperBook canonical type authority is unavailable")
        target_type = target if type(target) is type else type(target)
        if target_type is not canonical_type:
            raise ValueError("PaperBook authority target must be the canonical PaperBook type")

    return install, require


(
    _install_paperbook_type_authority,
    _require_paperbook_type_authority,
) = _make_paperbook_type_authority()


def _make_registry_book_key_authority():
    require_type = _require_paperbook_type_authority
    require_type_code = require_type.__code__

    def require(book: object) -> None:
        if require_type.__code__ is not require_type_code:
            raise ValueError("PaperBook canonical type authority changed")
        require_type(book)
        if require_type.__code__ is not require_type_code:
            raise ValueError("PaperBook canonical type authority changed")
        book_type = type(book)
        if (
            book_type.__hash__ is not object.__hash__
            or book_type.__eq__ is not object.__eq__
        ):
            raise ValueError("PaperBook registry key authority changed")

    return require


_require_registry_book_key_authority = _make_registry_book_key_authority()


def _ticket_opening_commitment(ticket: PaperTicket) -> tuple[object, ...]:
    """Return detached immutable opening facts that authorized PAPER economics."""
    leg_commitments = tuple(
        (
            leg.event_id,
            leg.market_id,
            leg.selection_id,
            leg.locked_odds,
            leg.sport,
            leg.exchange_side,
        )
        for leg in ticket.legs
    )
    return (
        ticket.stake,
        leg_commitments,
        ticket.placed_at,
        ticket.strategy_reason,
        ticket.provider_source_ids,
        ticket.provider_accounts,
        ticket.bankroll_id,
        ticket.currency,
    )


def _make_ticket_opening_authority_registry():
    # Opening economics are product-issued facts. Keep the authoritative copy
    # outside caller-visible PaperTicket fields so coherent field rewrites cannot
    # become their own witness.
    authorities = WeakKeyDictionary()
    guard = threading.RLock()
    require_registry_key = _require_registry_book_key_authority
    require_registry_key_code = require_registry_key.__code__
    commitment_for = _ticket_opening_commitment
    commitment_code = commitment_for.__code__

    def require_registry_key_authority(book: object) -> None:
        if require_registry_key.__code__ is not require_registry_key_code:
            raise ValueError("PaperBook registry key validator authority changed")
        require_registry_key(book)
        if require_registry_key.__code__ is not require_registry_key_code:
            raise ValueError("PaperBook registry key validator authority changed")

    def require_commitment_authority() -> None:
        if commitment_for.__code__ is not commitment_code:
            raise ValueError("PaperBook ticket opening commitment authority changed")

    def register_book(book: object) -> None:
        require_registry_key_authority(book)
        with guard:
            authorities[book] = {}

    def record(book: object, ticket: PaperTicket) -> None:
        require_registry_key_authority(book)
        require_commitment_authority()
        commitment = commitment_for(ticket)
        require_commitment_authority()
        with guard:
            current = authorities.get(book)
            if current is None:
                raise RuntimeError("PaperBook opening authority registry is unavailable")
            existing = current.get(ticket.ticket_id)
            if existing is not None and existing != commitment:
                raise ValueError("PaperBook ticket opening authority cannot be rebound")
            current[ticket.ticket_id] = commitment

    def revoke(book: object) -> None:
        require_registry_key_authority(book)
        with guard:
            authorities.pop(book, None)

    def install_validated_snapshot(book: object) -> None:
        require_registry_key_authority(book)
        require_commitment_authority()
        commitments = {
            ticket_id: commitment_for(ticket)
            for ticket_id, ticket in book.tickets.items()
        }
        require_commitment_authority()
        with guard:
            if book not in authorities:
                raise RuntimeError("PaperBook opening authority registry is unavailable")
            authorities[book] = commitments

    def require_current(book: object) -> None:
        require_registry_key_authority(book)
        require_commitment_authority()
        with guard:
            current = authorities.get(book)
            if current is None:
                raise ValueError(
                    "PaperBook byte-loaded snapshot lacks product-issued opening authority"
                )
            expected = dict(current)
        if set(expected) != set(book.tickets):
            raise ValueError(
                "PaperBook ticket set changed outside product-issued opening authority"
            )
        for ticket_id, ticket in book.tickets.items():
            commitment = commitment_for(ticket)
            require_commitment_authority()
            if expected[ticket_id] != commitment:
                raise ValueError(
                    "PaperBook ticket opening economic identity changed after admission"
                )

    def require_candidate(source_book: object, candidate_book: object) -> None:
        require_registry_key_authority(source_book)
        require_registry_key_authority(candidate_book)
        require_commitment_authority()
        with guard:
            current = authorities.get(source_book)
            if current is None:
                raise ValueError(
                    "PaperBook byte-loaded snapshot lacks product-issued opening authority"
                )
            expected = dict(current)
        candidate_tickets = getattr(candidate_book, "tickets", None)
        if type(candidate_tickets) is not dict or set(candidate_tickets) != set(expected):
            raise ValueError(
                "PaperBook serialized candidate ticket set differs from product-issued opening authority"
            )
        for ticket_id, ticket in candidate_tickets.items():
            if type(ticket) is not PaperTicket:
                raise ValueError(
                    "PaperBook serialized candidate opening economic identity differs from product-issued authority"
                )
            commitment = commitment_for(ticket)
            require_commitment_authority()
            if commitment != expected[ticket_id]:
                raise ValueError(
                    "PaperBook serialized candidate opening economic identity differs from product-issued authority"
                )

    return (
        register_book,
        record,
        revoke,
        install_validated_snapshot,
        require_current,
        require_candidate,
    )


(
    _register_ticket_opening_authority_book,
    _record_ticket_opening_authority,
    _revoke_ticket_opening_authority,
    _install_validated_ticket_opening_authority,
    _require_ticket_opening_authority,
    _require_snapshot_candidate_opening_authority,
) = _make_ticket_opening_authority_registry()


def _paperbook_causal_history_snapshot(book: object) -> tuple[object, ...]:
    lifecycle = getattr(book, "_lifecycle", None)
    settlement_times = getattr(book, "_settlement_times", None)
    if type(lifecycle) is not list or type(settlement_times) is not dict:
        raise ValueError("PaperBook causal history state is not canonical")
    return (
        tuple(lifecycle),
        tuple(sorted(settlement_times.items())),
    )


def _make_paperbook_causal_history_authority_registry():
    # Lifecycle/settlement chronology is product-issued economic history. Keep
    # the authoritative copy outside caller-visible mutable PaperBook fields.
    authorities = WeakKeyDictionary()
    guard = threading.RLock()
    require_registry_key = _require_registry_book_key_authority
    require_registry_key_code = require_registry_key.__code__
    snapshot_for = _paperbook_causal_history_snapshot
    snapshot_for_code = snapshot_for.__code__

    def require_registry_key_authority(book: object) -> None:
        if require_registry_key.__code__ is not require_registry_key_code:
            raise ValueError("PaperBook registry key validator authority changed")
        require_registry_key(book)
        if require_registry_key.__code__ is not require_registry_key_code:
            raise ValueError("PaperBook registry key validator authority changed")

    def require_snapshot_authority() -> None:
        if snapshot_for.__code__ is not snapshot_for_code:
            raise ValueError("PaperBook causal-history snapshot authority changed")

    def register_book(book: object) -> None:
        require_registry_key_authority(book)
        with guard:
            authorities[book] = ((), ())

    def revoke(book: object) -> None:
        require_registry_key_authority(book)
        with guard:
            authorities.pop(book, None)

    def install_validated_snapshot(book: object) -> None:
        require_registry_key_authority(book)
        require_snapshot_authority()
        snapshot = snapshot_for(book)
        require_snapshot_authority()
        with guard:
            authorities[book] = snapshot

    def require_current(book: object) -> None:
        require_registry_key_authority(book)
        require_snapshot_authority()
        actual = snapshot_for(book)
        require_snapshot_authority()
        with guard:
            expected = authorities.get(book)
        if expected is None:
            raise ValueError(
                "PaperBook byte-loaded snapshot lacks product-issued causal history authority"
            )
        if actual != expected:
            raise ValueError(
                "PaperBook causal history changed outside product-issued transitions"
            )

    def require_candidate(source_book: object, candidate_book: object) -> None:
        require_registry_key_authority(source_book)
        require_registry_key_authority(candidate_book)
        require_snapshot_authority()
        candidate = snapshot_for(candidate_book)
        require_snapshot_authority()
        with guard:
            expected = authorities.get(source_book)
        if expected is None:
            raise ValueError(
                "PaperBook byte-loaded snapshot lacks product-issued causal history authority"
            )
        if candidate != expected:
            raise ValueError(
                "PaperBook serialized candidate causal history differs from product-issued authority"
            )

    def advance_open(book: object, ticket_id: str) -> None:
        require_registry_key_authority(book)
        with guard:
            expected = authorities.get(book)
            if expected is None:
                raise ValueError(
                    "PaperBook byte-loaded snapshot lacks product-issued causal history authority"
                )
            lifecycle, settlement_times = expected
            authorities[book] = (
                lifecycle + (("open", ticket_id, (), ()),),
                settlement_times,
            )

    def advance_settle(
        book: object,
        ticket_id: str,
        winners: tuple[str, ...],
        voids: tuple[str, ...],
        settled_at: str | None,
    ) -> None:
        require_registry_key_authority(book)
        with guard:
            expected = authorities.get(book)
            if expected is None:
                raise ValueError(
                    "PaperBook byte-loaded snapshot lacks product-issued causal history authority"
                )
            lifecycle, settlement_times = expected
            settlement_mapping = dict(settlement_times)
            if ticket_id in settlement_mapping:
                raise ValueError(
                    "PaperBook causal history settlement authority cannot be rebound"
                )
            settlement_mapping[ticket_id] = settled_at
            authorities[book] = (
                lifecycle + (("settle", ticket_id, winners, voids),),
                tuple(sorted(settlement_mapping.items())),
            )

    return (
        register_book,
        revoke,
        install_validated_snapshot,
        require_current,
        require_candidate,
        advance_open,
        advance_settle,
    )


(
    _register_paperbook_causal_history_authority_book,
    _revoke_paperbook_causal_history_authority,
    _install_validated_paperbook_causal_history_authority,
    _require_paperbook_causal_history_authority,
    _require_snapshot_candidate_causal_history_authority,
    _advance_paperbook_causal_history_open,
    _advance_paperbook_causal_history_settle,
) = _make_paperbook_causal_history_authority_registry()




def _make_paperbook_operation_lock_registry():
    # Economic transitions/readouts must be linearizable per book. Key by object
    # identity so caller-defined __hash__/__eq__ can never run at the lock boundary.
    # Capture construction dependencies once so mutable module dispatch cannot run
    # caller-controlled code while a PaperBook is still establishing authority.
    entries: dict[int, tuple[object, threading.RLock]] = {}
    guard = threading.RLock()
    weak_ref_factory = ref
    lock_factory = threading.RLock
    lock_factory_code = getattr(lock_factory, "__code__", None)
    require_type = _require_paperbook_type_authority
    require_type_code = require_type.__code__

    def require_type_authority(book: object) -> None:
        if require_type.__code__ is not require_type_code:
            raise ValueError("PaperBook canonical type authority changed")
        require_type(book)
        if require_type.__code__ is not require_type_code:
            raise ValueError("PaperBook canonical type authority changed")

    def require_lock_factory_authority() -> None:
        if (
            lock_factory_code is not None
            and getattr(lock_factory, "__code__", None) is not lock_factory_code
        ):
            raise ValueError("PaperBook operation lock factory authority changed")

    def register_book(book: object) -> None:
        require_type_authority(book)
        identity = id(book)

        def cleanup(dead_ref: object, *, _identity: int = identity) -> None:
            with guard:
                current = entries.get(_identity)
                if current is not None and current[0] is dead_ref:
                    entries.pop(_identity, None)

        weak_book = weak_ref_factory(book, cleanup)
        require_lock_factory_authority()
        book_lock = lock_factory()
        require_lock_factory_authority()
        with guard:
            entries[identity] = (weak_book, book_lock)

    def require_lock(book: object) -> threading.RLock:
        require_type_authority(book)
        with guard:
            current = entries.get(id(book))
            if current is None or current[0]() is not book:
                raise RuntimeError("PaperBook operation lock registry is unavailable")
            return current[1]

    return register_book, require_lock


(
    _register_paperbook_operation_lock,
    _require_paperbook_operation_lock,
) = _make_paperbook_operation_lock_registry()


def _make_paperbook_visible_state_authority():
    validator = None
    validator_code = None

    def install(candidate) -> None:
        nonlocal validator, validator_code
        if validator is not None:
            raise RuntimeError("PaperBook visible-state validator authority already installed")
        if not callable(candidate) or not hasattr(candidate, "__code__"):
            raise TypeError("PaperBook visible-state validator authority must be a function")
        validator = candidate
        validator_code = candidate.__code__

    def require(book: object) -> None:
        if validator is None or validator_code is None:
            raise RuntimeError("PaperBook visible-state validator authority is unavailable")
        if validator.__code__ is not validator_code:
            raise ValueError("PaperBook visible-state validator authority changed")
        validator(type(book), book)
        if validator.__code__ is not validator_code:
            raise ValueError("PaperBook visible-state validator authority changed")

    return install, require


(
    _install_paperbook_visible_state_authority,
    _require_paperbook_visible_state_authority,
) = _make_paperbook_visible_state_authority()


def _serialized_paperbook_operation(method):
    require_lock = _require_paperbook_operation_lock
    require_lock_code = require_lock.__code__
    method_code = method.__code__

    @wraps(method)
    def serialized(self, *args, **kwargs):
        if method.__code__ is not method_code:
            raise ValueError("PaperBook operation callable authority changed")
        if require_lock.__code__ is not require_lock_code:
            raise ValueError("PaperBook operation lock authority changed")
        lock = require_lock(self)
        if require_lock.__code__ is not require_lock_code:
            raise ValueError("PaperBook operation lock authority changed")
        if method.__code__ is not method_code:
            raise ValueError("PaperBook operation callable authority changed")
        with lock:
            result = method(self, *args, **kwargs)
        if method.__code__ is not method_code:
            raise ValueError("PaperBook operation callable authority changed")
        return result

    # functools.wraps publishes the guarded callable through __wrapped__, which
    # would let callers bypass serialization and authority checks directly.
    del serialized.__wrapped__
    return serialized


def _guard_paperbook_runtime_authority(method):
    """Seal public PaperBook authority checks against module-level rebinding."""
    method_code = method.__code__
    visible_authority = _require_paperbook_visible_state_authority
    visible_authority_code = visible_authority.__code__
    opening_authority = _require_ticket_opening_authority
    opening_authority_code = opening_authority.__code__
    causal_authority = _require_paperbook_causal_history_authority
    causal_authority_code = causal_authority.__code__
    type_authority = _require_paperbook_type_authority
    type_authority_code = type_authority.__code__

    @wraps(method)
    def guarded(self, *args, **kwargs):
        if method.__code__ is not method_code:
            raise ValueError("PaperBook runtime callable authority changed")
        if type_authority.__code__ is not type_authority_code:
            raise ValueError("PaperBook canonical type authority changed")
        type_authority(self)
        if type_authority.__code__ is not type_authority_code:
            raise ValueError("PaperBook canonical type authority changed")
        # Preserve the existing safety order: canonical visible state must be
        # validated before hidden registries compare or hash caller-controlled
        # values. Invoke the closure-captured visible-state authority too, so
        # class/module rebinding cannot suppress canonical validation.
        if visible_authority.__code__ is not visible_authority_code:
            raise ValueError("PaperBook visible-state validator dispatch changed")
        visible_authority(self)
        if visible_authority.__code__ is not visible_authority_code:
            raise ValueError("PaperBook visible-state validator dispatch changed")
        if opening_authority.__code__ is not opening_authority_code:
            raise ValueError("PaperBook opening authority dispatch changed")
        opening_authority(self)
        if opening_authority.__code__ is not opening_authority_code:
            raise ValueError("PaperBook opening authority dispatch changed")
        if causal_authority.__code__ is not causal_authority_code:
            raise ValueError("PaperBook causal-history authority dispatch changed")
        causal_authority(self)
        if causal_authority.__code__ is not causal_authority_code:
            raise ValueError("PaperBook causal-history authority dispatch changed")
        if method.__code__ is not method_code:
            raise ValueError("PaperBook runtime callable authority changed")
        result = method(self, *args, **kwargs)
        if method.__code__ is not method_code:
            raise ValueError("PaperBook runtime callable authority changed")
        return result

    # Do not publish an unguarded entry point via functools.wraps.__wrapped__.
    del guarded.__wrapped__
    return guarded


def _make_paperbook_constructor_decimal_authority():
    descriptor = None
    function = None
    function_code = None

    def install(canonical_type: type) -> None:
        nonlocal descriptor, function, function_code
        if descriptor is not None:
            raise RuntimeError("PaperBook constructor decimal authority already installed")
        descriptor = canonical_type.__dict__["_canonical_decimal_input"]
        if type(descriptor) is not classmethod:
            raise TypeError("PaperBook canonical decimal helper must remain a classmethod")
        function = descriptor.__func__
        function_code = function.__code__

    def canonicalize(book: object, value: object, label: str) -> Decimal:
        if descriptor is None or function is None or function_code is None:
            raise RuntimeError("PaperBook constructor decimal authority is unavailable")
        canonical_type = type(book)
        if canonical_type.__dict__.get("_canonical_decimal_input") is not descriptor:
            raise ValueError("PaperBook constructor decimal dispatch changed")
        if function.__code__ is not function_code:
            raise ValueError("PaperBook constructor decimal authority changed")
        result = function(canonical_type, value, label)
        if function.__code__ is not function_code:
            raise ValueError("PaperBook constructor decimal authority changed")
        return result

    return install, canonicalize


(
    _install_paperbook_constructor_decimal_authority,
    _canonicalize_paperbook_constructor_decimal,
) = _make_paperbook_constructor_decimal_authority()


def _guard_paperbook_constructor_authority(method):
    """Seal PaperBook authority registration before instance state is accepted."""
    method_code = method.__code__
    operation_register = _register_paperbook_operation_lock
    operation_register_code = operation_register.__code__
    opening_register = _register_ticket_opening_authority_book
    opening_register_code = opening_register.__code__
    causal_register = _register_paperbook_causal_history_authority_book
    causal_register_code = causal_register.__code__
    type_authority = _require_paperbook_type_authority
    type_authority_code = type_authority.__code__
    canonical_decimal = _canonicalize_paperbook_constructor_decimal
    canonical_decimal_code = canonical_decimal.__code__

    def require_type(book: object) -> None:
        if type_authority.__code__ is not type_authority_code:
            raise ValueError("PaperBook canonical type authority changed")
        type_authority(book)
        if type_authority.__code__ is not type_authority_code:
            raise ValueError("PaperBook canonical type authority changed")

    def invoke(registrar, expected_code, label: str, book: object) -> None:
        if registrar.__code__ is not expected_code:
            raise ValueError(f"PaperBook {label} constructor authority changed")
        registrar(book)
        if registrar.__code__ is not expected_code:
            raise ValueError(f"PaperBook {label} constructor authority changed")

    @wraps(method)
    def guarded(self, *args, **kwargs):
        if method.__code__ is not method_code:
            raise ValueError("PaperBook constructor callable authority changed")
        require_type(self)
        invoke(
            operation_register,
            operation_register_code,
            "operation lock",
            self,
        )
        invoke(
            opening_register,
            opening_register_code,
            "opening registry",
            self,
        )
        invoke(
            causal_register,
            causal_register_code,
            "causal-history registry",
            self,
        )
        if method.__code__ is not method_code:
            raise ValueError("PaperBook constructor callable authority changed")
        if canonical_decimal.__code__ is not canonical_decimal_code:
            raise ValueError("PaperBook constructor decimal dispatch authority changed")
        result = method(
            self,
            *args,
            _canonical_decimal=canonical_decimal,
            **kwargs,
        )
        if method.__code__ is not method_code:
            raise ValueError("PaperBook constructor callable authority changed")
        return result

    # Constructor registration is part of the authority boundary; exposing the
    # original __init__ through __wrapped__ would permit registry-free objects.
    del guarded.__wrapped__
    return guarded


def _paper_decimal_context() -> Context:
    context = Context(
        prec=_PAPER_DECIMAL_PRECISION,
        rounding=ROUND_HALF_EVEN,
        Emin=_PAPER_DECIMAL_EMIN,
        Emax=_PAPER_DECIMAL_EMAX,
    )
    context.traps[InvalidOperation] = True
    context.traps[Overflow] = True
    context.traps[Underflow] = True
    context.clear_flags()
    return context


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"PaperBook snapshot contains duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"PaperBook snapshot contains non-finite JSON constant: {value}")


def _make_paperbook_snapshot_entry_dispatch_authority():
    raw_snapshot_descriptor = None
    raw_snapshot_function = None
    raw_snapshot_code = None
    load_bytes_descriptor = None
    load_bytes_function = None
    load_bytes_code = None
    snapshot_path_descriptor = None
    snapshot_path_function = None
    snapshot_path_code = None
    lifecycle_json_descriptor = None
    lifecycle_json_function = None
    lifecycle_json_code = None
    ensure_parent_descriptor = None
    ensure_parent_function = None
    ensure_parent_code = None
    fsync_directory_descriptor = None
    fsync_directory_function = None
    fsync_directory_code = None
    snapshot_helper_authorities = None
    snapshot_helper_names = (
        "_require_finite",
        "_require_canonical_text",
        "_validate_timestamp",
        "_validate_ticket_leg",
        "_validate_lifecycle_entry",
        "_validate_lifecycle_reachability",
        "_validate_loaded_state",
        "_parse_lifecycle_key_list",
        "_parse_lifecycle",
        "_parse_snapshot_decimal",
        "_required_snapshot_field",
        "_parse_snapshot_legs",
        "_parse_snapshot_provider_accounts",
        "_parse_snapshot_status",
    )

    def install(canonical_type: type) -> None:
        nonlocal raw_snapshot_descriptor, raw_snapshot_function, raw_snapshot_code
        nonlocal load_bytes_descriptor, load_bytes_function, load_bytes_code
        nonlocal snapshot_path_descriptor, snapshot_path_function, snapshot_path_code
        nonlocal lifecycle_json_descriptor, lifecycle_json_function, lifecycle_json_code
        nonlocal ensure_parent_descriptor, ensure_parent_function, ensure_parent_code
        nonlocal fsync_directory_descriptor, fsync_directory_function, fsync_directory_code
        nonlocal snapshot_helper_authorities
        if raw_snapshot_descriptor is not None:
            raise RuntimeError("PaperBook snapshot entry dispatch authority already installed")
        raw_snapshot_descriptor = canonical_type.__dict__["_from_raw_snapshot"]
        load_bytes_descriptor = canonical_type.__dict__["load_bytes"]
        snapshot_path_descriptor = canonical_type.__dict__["_canonical_snapshot_path"]
        lifecycle_json_descriptor = canonical_type.__dict__["_lifecycle_to_json"]
        ensure_parent_descriptor = canonical_type.__dict__["_ensure_snapshot_parent_durable"]
        fsync_directory_descriptor = canonical_type.__dict__["_fsync_snapshot_directory"]
        if type(raw_snapshot_descriptor) is not classmethod:
            raise TypeError("PaperBook raw snapshot decoder must remain a classmethod")
        if type(load_bytes_descriptor) is not classmethod:
            raise TypeError("PaperBook byte loader must remain a classmethod")
        if type(snapshot_path_descriptor) is not staticmethod:
            raise TypeError("PaperBook snapshot path helper must remain a staticmethod")
        if type(lifecycle_json_descriptor) is not FunctionType:
            raise TypeError("PaperBook lifecycle serializer must remain an instance method")
        if type(ensure_parent_descriptor) is not classmethod:
            raise TypeError("PaperBook parent durability helper must remain a classmethod")
        if type(fsync_directory_descriptor) is not staticmethod:
            raise TypeError("PaperBook directory fsync helper must remain a staticmethod")
        raw_snapshot_function = raw_snapshot_descriptor.__func__
        load_bytes_function = load_bytes_descriptor.__func__
        snapshot_path_function = snapshot_path_descriptor.__func__
        lifecycle_json_function = lifecycle_json_descriptor
        ensure_parent_function = ensure_parent_descriptor.__func__
        fsync_directory_function = fsync_directory_descriptor.__func__
        raw_snapshot_code = raw_snapshot_function.__code__
        load_bytes_code = load_bytes_function.__code__
        snapshot_path_code = snapshot_path_function.__code__
        lifecycle_json_code = lifecycle_json_function.__code__
        ensure_parent_code = ensure_parent_function.__code__
        fsync_directory_code = fsync_directory_function.__code__
        helper_authorities = {}
        for helper_name in snapshot_helper_names:
            helper_descriptor = canonical_type.__dict__.get(helper_name)
            if helper_descriptor is None:
                raise RuntimeError(
                    f"PaperBook snapshot helper {helper_name} is unavailable"
                )
            if type(helper_descriptor) in {classmethod, staticmethod}:
                helper_function = helper_descriptor.__func__
            else:
                helper_function = helper_descriptor
            helper_code = getattr(helper_function, "__code__", None)
            if helper_code is None:
                raise TypeError(
                    f"PaperBook snapshot helper {helper_name} must be a Python callable"
                )
            helper_authorities[helper_name] = (
                helper_descriptor,
                helper_function,
                helper_code,
            )
        snapshot_helper_authorities = helper_authorities

    def require_snapshot_helpers(canonical_type: type) -> None:
        if snapshot_helper_authorities is None:
            raise RuntimeError("PaperBook snapshot helper authority is unavailable")
        for helper_name, (
            expected_descriptor,
            helper_function,
            expected_code,
        ) in snapshot_helper_authorities.items():
            if canonical_type.__dict__.get(helper_name) is not expected_descriptor:
                raise ValueError(
                    f"PaperBook snapshot helper dispatch changed: {helper_name}"
                )
            if helper_function.__code__ is not expected_code:
                raise ValueError(
                    f"PaperBook snapshot helper authority changed: {helper_name}"
                )

    def decode_raw(canonical_type: type, raw: object):
        if raw_snapshot_descriptor is None or raw_snapshot_function is None or raw_snapshot_code is None:
            raise RuntimeError("PaperBook raw snapshot dispatch authority is unavailable")
        if canonical_type.__dict__.get("_from_raw_snapshot") is not raw_snapshot_descriptor:
            raise ValueError("PaperBook raw snapshot decoder dispatch changed")
        if raw_snapshot_function.__code__ is not raw_snapshot_code:
            raise ValueError("PaperBook raw snapshot decoder authority changed")
        require_snapshot_helpers(canonical_type)
        result = raw_snapshot_function(canonical_type, raw)
        require_snapshot_helpers(canonical_type)
        if raw_snapshot_function.__code__ is not raw_snapshot_code:
            raise ValueError("PaperBook raw snapshot decoder authority changed")
        return result

    def decode_bytes(canonical_type: type, payload: bytes):
        if load_bytes_descriptor is None or load_bytes_function is None or load_bytes_code is None:
            raise RuntimeError("PaperBook byte loader dispatch authority is unavailable")
        if canonical_type.__dict__.get("load_bytes") is not load_bytes_descriptor:
            raise ValueError("PaperBook byte loader dispatch changed")
        if load_bytes_function.__code__ is not load_bytes_code:
            raise ValueError("PaperBook byte loader authority changed")
        result = load_bytes_function(canonical_type, payload)
        if load_bytes_function.__code__ is not load_bytes_code:
            raise ValueError("PaperBook byte loader authority changed")
        return result

    def canonical_path(canonical_type: type, path: object):
        if snapshot_path_descriptor is None or snapshot_path_function is None or snapshot_path_code is None:
            raise RuntimeError("PaperBook snapshot path dispatch authority is unavailable")
        if canonical_type.__dict__.get("_canonical_snapshot_path") is not snapshot_path_descriptor:
            raise ValueError("PaperBook snapshot path dispatch changed")
        if snapshot_path_function.__code__ is not snapshot_path_code:
            raise ValueError("PaperBook snapshot path authority changed")
        result = snapshot_path_function(path)
        if snapshot_path_function.__code__ is not snapshot_path_code:
            raise ValueError("PaperBook snapshot path authority changed")
        return result

    def lifecycle_json(canonical_type: type, book: object):
        if lifecycle_json_descriptor is None or lifecycle_json_function is None or lifecycle_json_code is None:
            raise RuntimeError("PaperBook lifecycle serializer dispatch authority is unavailable")
        if canonical_type.__dict__.get("_lifecycle_to_json") is not lifecycle_json_descriptor:
            raise ValueError("PaperBook lifecycle serializer dispatch changed")
        if lifecycle_json_function.__code__ is not lifecycle_json_code:
            raise ValueError("PaperBook lifecycle serializer authority changed")
        result = lifecycle_json_function(book)
        if lifecycle_json_function.__code__ is not lifecycle_json_code:
            raise ValueError("PaperBook lifecycle serializer authority changed")
        return result

    def ensure_parent(canonical_type: type, directory: Path) -> None:
        if ensure_parent_descriptor is None or ensure_parent_function is None or ensure_parent_code is None:
            raise RuntimeError("PaperBook parent durability dispatch authority is unavailable")
        if canonical_type.__dict__.get("_ensure_snapshot_parent_durable") is not ensure_parent_descriptor:
            raise ValueError("PaperBook parent durability dispatch changed")
        if ensure_parent_function.__code__ is not ensure_parent_code:
            raise ValueError("PaperBook parent durability authority changed")
        ensure_parent_function(canonical_type, directory)
        if ensure_parent_function.__code__ is not ensure_parent_code:
            raise ValueError("PaperBook parent durability authority changed")

    def fsync_directory(canonical_type: type, directory: Path) -> None:
        if fsync_directory_descriptor is None or fsync_directory_function is None or fsync_directory_code is None:
            raise RuntimeError("PaperBook directory fsync dispatch authority is unavailable")
        if canonical_type.__dict__.get("_fsync_snapshot_directory") is not fsync_directory_descriptor:
            raise ValueError("PaperBook directory fsync dispatch changed")
        if fsync_directory_function.__code__ is not fsync_directory_code:
            raise ValueError("PaperBook directory fsync authority changed")
        fsync_directory_function(directory)
        if fsync_directory_function.__code__ is not fsync_directory_code:
            raise ValueError("PaperBook directory fsync authority changed")

    return (
        install,
        decode_raw,
        decode_bytes,
        canonical_path,
        lifecycle_json,
        ensure_parent,
        fsync_directory,
    )


(
    _install_paperbook_snapshot_entry_dispatch_authority,
    _decode_canonical_paperbook_raw_snapshot,
    _decode_canonical_paperbook_bytes,
    _canonical_paperbook_snapshot_path,
    _canonical_paperbook_lifecycle_json,
    _ensure_canonical_paperbook_snapshot_parent,
    _fsync_canonical_paperbook_snapshot_directory,
) = _make_paperbook_snapshot_entry_dispatch_authority()


def _make_paperbook_economic_helper_dispatch_authority():
    decimal_descriptor = None
    decimal_function = None
    decimal_code = None
    debit_descriptor = None
    debit_function = None
    debit_code = None
    settlement_descriptor = None
    settlement_function = None
    settlement_code = None

    def install(canonical_type: type) -> None:
        nonlocal decimal_descriptor, decimal_function, decimal_code
        nonlocal debit_descriptor, debit_function, debit_code
        nonlocal settlement_descriptor, settlement_function, settlement_code
        if decimal_descriptor is not None:
            raise RuntimeError("PaperBook economic helper dispatch authority already installed")
        decimal_descriptor = canonical_type.__dict__["_canonical_decimal_input"]
        debit_descriptor = canonical_type.__dict__["_debit_balance"]
        settlement_descriptor = canonical_type.__dict__["_settlement_result"]
        if type(decimal_descriptor) is not classmethod:
            raise TypeError("PaperBook decimal helper must remain a classmethod")
        if type(debit_descriptor) is not classmethod:
            raise TypeError("PaperBook debit helper must remain a classmethod")
        if type(settlement_descriptor) is not classmethod:
            raise TypeError("PaperBook settlement helper must remain a classmethod")
        decimal_function = decimal_descriptor.__func__
        debit_function = debit_descriptor.__func__
        settlement_function = settlement_descriptor.__func__
        decimal_code = decimal_function.__code__
        debit_code = debit_function.__code__
        settlement_code = settlement_function.__code__

    def canonical_decimal(canonical_type: type, value: object, label: str):
        if decimal_descriptor is None or decimal_function is None or decimal_code is None:
            raise RuntimeError("PaperBook decimal helper dispatch authority is unavailable")
        if canonical_type.__dict__.get("_canonical_decimal_input") is not decimal_descriptor:
            raise ValueError("PaperBook decimal helper dispatch changed")
        if decimal_function.__code__ is not decimal_code:
            raise ValueError("PaperBook decimal helper authority changed")
        result = decimal_function(canonical_type, value, label)
        if decimal_function.__code__ is not decimal_code:
            raise ValueError("PaperBook decimal helper authority changed")
        return result

    def debit_balance(canonical_type: type, balance: Decimal, amount: Decimal):
        if debit_descriptor is None or debit_function is None or debit_code is None:
            raise RuntimeError("PaperBook debit helper dispatch authority is unavailable")
        if canonical_type.__dict__.get("_debit_balance") is not debit_descriptor:
            raise ValueError("PaperBook debit helper dispatch changed")
        if debit_function.__code__ is not debit_code:
            raise ValueError("PaperBook debit helper authority changed")
        result = debit_function(canonical_type, balance, amount)
        if debit_function.__code__ is not debit_code:
            raise ValueError("PaperBook debit helper authority changed")
        return result

    def settlement_result(
        canonical_type: type,
        ticket: PaperTicket,
        balance: Decimal,
        winning_quote_keys: set[str],
        void_quote_keys: set[str],
    ):
        if settlement_descriptor is None or settlement_function is None or settlement_code is None:
            raise RuntimeError("PaperBook settlement helper dispatch authority is unavailable")
        if canonical_type.__dict__.get("_settlement_result") is not settlement_descriptor:
            raise ValueError("PaperBook settlement helper dispatch changed")
        if settlement_function.__code__ is not settlement_code:
            raise ValueError("PaperBook settlement helper authority changed")
        result = settlement_function(
            canonical_type,
            ticket,
            balance,
            winning_quote_keys,
            void_quote_keys,
        )
        if settlement_function.__code__ is not settlement_code:
            raise ValueError("PaperBook settlement helper authority changed")
        return result

    return install, canonical_decimal, debit_balance, settlement_result


(
    _install_paperbook_economic_helper_dispatch_authority,
    _canonical_paperbook_decimal_input,
    _canonical_paperbook_debit_balance,
    _canonical_paperbook_settlement_result,
) = _make_paperbook_economic_helper_dispatch_authority()


def _seal_paperbook_open_transition_authority(method):
    """Inject closure-captured write authorities into open_ticket."""
    method_code = method.__code__
    opening_record = _record_ticket_opening_authority
    opening_record_code = opening_record.__code__
    causal_advance = _advance_paperbook_causal_history_open
    causal_advance_code = causal_advance.__code__
    ticket_id_factory = uuid.uuid4
    ticket_id_factory_code = ticket_id_factory.__code__
    placed_at_now = utc_now_iso
    placed_at_now_code = placed_at_now.__code__
    ticket_type = PaperTicket
    ticket_init = ticket_type.__init__
    ticket_init_code = ticket_init.__code__
    canonical_decimal = _canonical_paperbook_decimal_input
    canonical_decimal_code = canonical_decimal.__code__
    debit_balance = _canonical_paperbook_debit_balance
    debit_balance_code = debit_balance.__code__

    def issue_ticket_id() -> str:
        if ticket_id_factory.__code__ is not ticket_id_factory_code:
            raise ValueError("PaperBook ticket id authority changed")
        ticket_id = str(ticket_id_factory())
        if ticket_id_factory.__code__ is not ticket_id_factory_code:
            raise ValueError("PaperBook ticket id authority changed")
        return ticket_id

    def current_timestamp() -> str:
        if placed_at_now.__code__ is not placed_at_now_code:
            raise ValueError("PaperBook placed_at clock authority changed")
        timestamp = placed_at_now()
        if placed_at_now.__code__ is not placed_at_now_code:
            raise ValueError("PaperBook placed_at clock authority changed")
        return timestamp

    def build_ticket(**kwargs) -> PaperTicket:
        if ticket_type.__init__ is not ticket_init or ticket_init.__code__ is not ticket_init_code:
            raise ValueError("PaperBook ticket constructor authority changed")
        ticket = ticket_type(**kwargs)
        if ticket_type.__init__ is not ticket_init or ticket_init.__code__ is not ticket_init_code:
            raise ValueError("PaperBook ticket constructor authority changed")
        return ticket

    def record_opening(book: object, ticket: PaperTicket) -> None:
        if opening_record.__code__ is not opening_record_code:
            raise ValueError("PaperBook opening write authority changed")
        opening_record(book, ticket)
        if opening_record.__code__ is not opening_record_code:
            raise ValueError("PaperBook opening write authority changed")

    def advance_open(book: object, ticket_id: str) -> None:
        if causal_advance.__code__ is not causal_advance_code:
            raise ValueError("PaperBook causal-history open write authority changed")
        causal_advance(book, ticket_id)
        if causal_advance.__code__ is not causal_advance_code:
            raise ValueError("PaperBook causal-history open write authority changed")

    @wraps(method)
    def sealed(self, *args, **kwargs):
        if method.__code__ is not method_code:
            raise ValueError("PaperBook open callable authority changed")
        if opening_record.__code__ is not opening_record_code:
            raise ValueError("PaperBook opening write authority changed")
        if causal_advance.__code__ is not causal_advance_code:
            raise ValueError("PaperBook causal-history open write authority changed")
        if ticket_id_factory.__code__ is not ticket_id_factory_code:
            raise ValueError("PaperBook ticket id authority changed")
        if placed_at_now.__code__ is not placed_at_now_code:
            raise ValueError("PaperBook placed_at clock authority changed")
        if ticket_type.__init__ is not ticket_init or ticket_init.__code__ is not ticket_init_code:
            raise ValueError("PaperBook ticket constructor authority changed")
        if canonical_decimal.__code__ is not canonical_decimal_code:
            raise ValueError("PaperBook decimal helper dispatch authority changed")
        if debit_balance.__code__ is not debit_balance_code:
            raise ValueError("PaperBook debit helper dispatch authority changed")
        result = method(
            self,
            *args,
            _opening_authority_record=record_opening,
            _causal_open_advance=advance_open,
            _ticket_id_factory=issue_ticket_id,
            _placed_at_now=current_timestamp,
            _ticket_factory=build_ticket,
            _canonical_decimal=canonical_decimal,
            _debit_balance=debit_balance,
            **kwargs,
        )
        if method.__code__ is not method_code:
            raise ValueError("PaperBook open callable authority changed")
        return result

    del sealed.__wrapped__
    return sealed


def _seal_paperbook_json_decode_authority(method):
    """Inject closure-captured JSON parser and rejection hooks into load_bytes."""
    method_code = method.__code__
    loads = json.loads
    loads_code = loads.__code__
    duplicate_hook = _reject_duplicate_json_keys
    duplicate_hook_code = duplicate_hook.__code__
    constant_hook = _reject_nonfinite_json_constant
    constant_hook_code = constant_hook.__code__
    raw_snapshot_decode = _decode_canonical_paperbook_raw_snapshot
    raw_snapshot_decode_code = raw_snapshot_decode.__code__
    type_authority = _require_paperbook_type_authority
    type_authority_code = type_authority.__code__

    def require_type(target: object) -> None:
        if type_authority.__code__ is not type_authority_code:
            raise ValueError("PaperBook canonical type authority changed")
        type_authority(target)
        if type_authority.__code__ is not type_authority_code:
            raise ValueError("PaperBook canonical type authority changed")

    def decode(text: str) -> object:
        if loads.__code__ is not loads_code:
            raise ValueError("PaperBook JSON parser authority changed")
        if duplicate_hook.__code__ is not duplicate_hook_code:
            raise ValueError("PaperBook duplicate-key authority changed")
        if constant_hook.__code__ is not constant_hook_code:
            raise ValueError("PaperBook non-finite constant authority changed")
        raw = loads(
            text,
            object_pairs_hook=duplicate_hook,
            parse_constant=constant_hook,
        )
        if loads.__code__ is not loads_code:
            raise ValueError("PaperBook JSON parser authority changed")
        if duplicate_hook.__code__ is not duplicate_hook_code:
            raise ValueError("PaperBook duplicate-key authority changed")
        if constant_hook.__code__ is not constant_hook_code:
            raise ValueError("PaperBook non-finite constant authority changed")
        return raw

    @wraps(method)
    def sealed(cls, *args, **kwargs):
        require_type(cls)
        if method.__code__ is not method_code:
            raise ValueError("PaperBook JSON decode callable authority changed")
        if loads.__code__ is not loads_code:
            raise ValueError("PaperBook JSON parser authority changed")
        if duplicate_hook.__code__ is not duplicate_hook_code:
            raise ValueError("PaperBook duplicate-key authority changed")
        if constant_hook.__code__ is not constant_hook_code:
            raise ValueError("PaperBook non-finite constant authority changed")
        if raw_snapshot_decode.__code__ is not raw_snapshot_decode_code:
            raise ValueError("PaperBook raw snapshot dispatch authority changed")
        result = method(
            cls,
            *args,
            _json_decode=decode,
            _raw_snapshot_decode=raw_snapshot_decode,
            **kwargs,
        )
        if method.__code__ is not method_code:
            raise ValueError("PaperBook JSON decode callable authority changed")
        return result

    del sealed.__wrapped__
    return sealed


def _seal_paperbook_snapshot_decode_authority(method):
    """Inject closure-captured authority revocation into raw snapshot decoding."""
    method_code = method.__code__
    revoke_opening = _revoke_ticket_opening_authority
    revoke_opening_code = revoke_opening.__code__
    revoke_causal = _revoke_paperbook_causal_history_authority
    revoke_causal_code = revoke_causal.__code__
    type_authority = _require_paperbook_type_authority
    type_authority_code = type_authority.__code__

    def require_type(target: object) -> None:
        if type_authority.__code__ is not type_authority_code:
            raise ValueError("PaperBook canonical type authority changed")
        type_authority(target)
        if type_authority.__code__ is not type_authority_code:
            raise ValueError("PaperBook canonical type authority changed")

    def revoke(book: object) -> None:
        if revoke_opening.__code__ is not revoke_opening_code:
            raise ValueError("PaperBook opening revoke authority changed")
        if revoke_causal.__code__ is not revoke_causal_code:
            raise ValueError("PaperBook causal-history revoke authority changed")
        revoke_opening(book)
        if revoke_opening.__code__ is not revoke_opening_code:
            raise ValueError("PaperBook opening revoke authority changed")
        revoke_causal(book)
        if revoke_causal.__code__ is not revoke_causal_code:
            raise ValueError("PaperBook causal-history revoke authority changed")

    @wraps(method)
    def sealed(cls, *args, **kwargs):
        require_type(cls)
        if method.__code__ is not method_code:
            raise ValueError("PaperBook snapshot decode callable authority changed")
        if revoke_opening.__code__ is not revoke_opening_code:
            raise ValueError("PaperBook opening revoke authority changed")
        if revoke_causal.__code__ is not revoke_causal_code:
            raise ValueError("PaperBook causal-history revoke authority changed")
        result = method(cls, *args, _snapshot_authority_revoke=revoke, **kwargs)
        if method.__code__ is not method_code:
            raise ValueError("PaperBook snapshot decode callable authority changed")
        return result

    del sealed.__wrapped__
    return sealed


def _seal_paperbook_snapshot_install_authority(method):
    """Inject closure-captured authority installation into trusted file load."""
    method_code = method.__code__
    install_opening = _install_validated_ticket_opening_authority
    install_opening_code = install_opening.__code__
    install_causal = _install_validated_paperbook_causal_history_authority
    install_causal_code = install_causal.__code__
    type_authority = _require_paperbook_type_authority
    type_authority_code = type_authority.__code__
    canonical_load_bytes = _decode_canonical_paperbook_bytes
    canonical_load_bytes_code = canonical_load_bytes.__code__
    canonical_snapshot_path = _canonical_paperbook_snapshot_path
    canonical_snapshot_path_code = canonical_snapshot_path.__code__
    lifecycle_json = _canonical_paperbook_lifecycle_json
    lifecycle_json_code = lifecycle_json.__code__
    ensure_parent = _ensure_canonical_paperbook_snapshot_parent
    ensure_parent_code = ensure_parent.__code__
    fsync_directory = _fsync_canonical_paperbook_snapshot_directory
    fsync_directory_code = fsync_directory.__code__

    def require_type(target: object) -> None:
        if type_authority.__code__ is not type_authority_code:
            raise ValueError("PaperBook canonical type authority changed")
        type_authority(target)
        if type_authority.__code__ is not type_authority_code:
            raise ValueError("PaperBook canonical type authority changed")

    def install(book: object) -> None:
        if install_opening.__code__ is not install_opening_code:
            raise ValueError("PaperBook opening install authority changed")
        if install_causal.__code__ is not install_causal_code:
            raise ValueError("PaperBook causal-history install authority changed")
        install_opening(book)
        if install_opening.__code__ is not install_opening_code:
            raise ValueError("PaperBook opening install authority changed")
        install_causal(book)
        if install_causal.__code__ is not install_causal_code:
            raise ValueError("PaperBook causal-history install authority changed")

    @wraps(method)
    def sealed(cls, *args, **kwargs):
        require_type(cls)
        if method.__code__ is not method_code:
            raise ValueError("PaperBook snapshot install callable authority changed")
        if install_opening.__code__ is not install_opening_code:
            raise ValueError("PaperBook opening install authority changed")
        if install_causal.__code__ is not install_causal_code:
            raise ValueError("PaperBook causal-history install authority changed")
        if canonical_load_bytes.__code__ is not canonical_load_bytes_code:
            raise ValueError("PaperBook byte loader dispatch authority changed")
        if canonical_snapshot_path.__code__ is not canonical_snapshot_path_code:
            raise ValueError("PaperBook snapshot path dispatch authority changed")
        if lifecycle_json.__code__ is not lifecycle_json_code:
            raise ValueError("PaperBook lifecycle serializer dispatch authority changed")
        if ensure_parent.__code__ is not ensure_parent_code:
            raise ValueError("PaperBook parent durability dispatch authority changed")
        if fsync_directory.__code__ is not fsync_directory_code:
            raise ValueError("PaperBook directory fsync dispatch authority changed")
        result = method(
            cls,
            *args,
            _snapshot_authority_install=install,
            _canonical_load_bytes=canonical_load_bytes,
            _snapshot_path=canonical_snapshot_path,
            **kwargs,
        )
        if method.__code__ is not method_code:
            raise ValueError("PaperBook snapshot install callable authority changed")
        return result

    del sealed.__wrapped__
    return sealed


def _seal_paperbook_save_candidate_authority(method):
    """Inject closure-captured snapshot candidate and dispatch authorities into save."""
    method_code = method.__code__
    opening_candidate = _require_snapshot_candidate_opening_authority
    opening_candidate_code = opening_candidate.__code__
    causal_candidate = _require_snapshot_candidate_causal_history_authority
    causal_candidate_code = causal_candidate.__code__
    canonical_raw_snapshot = _decode_canonical_paperbook_raw_snapshot
    canonical_raw_snapshot_code = canonical_raw_snapshot.__code__
    canonical_snapshot_path = _canonical_paperbook_snapshot_path
    canonical_snapshot_path_code = canonical_snapshot_path.__code__

    def require_opening(source_book: object, candidate_book: object) -> None:
        if opening_candidate.__code__ is not opening_candidate_code:
            raise ValueError("PaperBook snapshot opening candidate authority changed")
        opening_candidate(source_book, candidate_book)
        if opening_candidate.__code__ is not opening_candidate_code:
            raise ValueError("PaperBook snapshot opening candidate authority changed")

    def require_causal(source_book: object, candidate_book: object) -> None:
        if causal_candidate.__code__ is not causal_candidate_code:
            raise ValueError("PaperBook snapshot causal candidate authority changed")
        causal_candidate(source_book, candidate_book)
        if causal_candidate.__code__ is not causal_candidate_code:
            raise ValueError("PaperBook snapshot causal candidate authority changed")

    @wraps(method)
    def sealed(self, *args, **kwargs):
        if method.__code__ is not method_code:
            raise ValueError("PaperBook save candidate callable authority changed")
        if opening_candidate.__code__ is not opening_candidate_code:
            raise ValueError("PaperBook snapshot opening candidate authority changed")
        if causal_candidate.__code__ is not causal_candidate_code:
            raise ValueError("PaperBook snapshot causal candidate authority changed")
        if canonical_raw_snapshot.__code__ is not canonical_raw_snapshot_code:
            raise ValueError("PaperBook raw snapshot dispatch authority changed")
        if canonical_snapshot_path.__code__ is not canonical_snapshot_path_code:
            raise ValueError("PaperBook snapshot path dispatch authority changed")
        result = method(
            self,
            *args,
            _opening_candidate_authority=require_opening,
            _causal_candidate_authority=require_causal,
            _canonical_snapshot_decode=canonical_raw_snapshot,
            _snapshot_path=canonical_snapshot_path,
            _lifecycle_json=lifecycle_json,
            _ensure_parent_durable=ensure_parent,
            _fsync_directory=fsync_directory,
            **kwargs,
        )
        if method.__code__ is not method_code:
            raise ValueError("PaperBook save candidate callable authority changed")
        return result

    del sealed.__wrapped__
    return sealed



def _seal_paperbook_snapshot_json_publish_authority(method):
    """Inject closure-captured JSON serialization authority into durable save."""
    method_code = method.__code__
    dump = json.dump
    dump_code = dump.__code__

    def publish(raw: object, handle: object) -> None:
        if dump.__code__ is not dump_code:
            raise ValueError("PaperBook JSON serializer authority changed")
        dump(raw, handle, ensure_ascii=False, indent=2)
        if dump.__code__ is not dump_code:
            raise ValueError("PaperBook JSON serializer authority changed")

    @wraps(method)
    def sealed(self, *args, **kwargs):
        if method.__code__ is not method_code:
            raise ValueError("PaperBook save publish callable authority changed")
        if dump.__code__ is not dump_code:
            raise ValueError("PaperBook JSON serializer authority changed")
        result = method(self, *args, _json_dump=publish, **kwargs)
        if method.__code__ is not method_code:
            raise ValueError("PaperBook save publish callable authority changed")
        return result

    del sealed.__wrapped__
    return sealed


def _seal_paperbook_settle_transition_authority(method):
    """Inject closure-captured write and economic authorities into settle."""
    method_code = method.__code__
    causal_advance = _advance_paperbook_causal_history_settle
    causal_advance_code = causal_advance.__code__
    settlement_result = _canonical_paperbook_settlement_result
    settlement_result_code = settlement_result.__code__

    def advance_settle(
        book: object,
        ticket_id: str,
        winners: tuple[str, ...],
        voids: tuple[str, ...],
        settled_at: str | None,
    ) -> None:
        if causal_advance.__code__ is not causal_advance_code:
            raise ValueError("PaperBook causal-history settle write authority changed")
        causal_advance(book, ticket_id, winners, voids, settled_at)
        if causal_advance.__code__ is not causal_advance_code:
            raise ValueError("PaperBook causal-history settle write authority changed")

    @wraps(method)
    def sealed(self, *args, **kwargs):
        if method.__code__ is not method_code:
            raise ValueError("PaperBook settle callable authority changed")
        if causal_advance.__code__ is not causal_advance_code:
            raise ValueError("PaperBook causal-history settle write authority changed")
        if settlement_result.__code__ is not settlement_result_code:
            raise ValueError("PaperBook settlement helper dispatch authority changed")
        result = method(
            self,
            *args,
            _causal_settle_advance=advance_settle,
            _settlement_result=settlement_result,
            **kwargs,
        )
        if method.__code__ is not method_code:
            raise ValueError("PaperBook settle callable authority changed")
        return result

    del sealed.__wrapped__
    return sealed


class PaperBook:
    """Virtual bankroll and auditable paper tickets. No real-money execution path exists."""

    @_guard_paperbook_constructor_authority
    def __init__(
        self,
        initial_bankroll: Decimal | str = Decimal("10000"),
        *,
        _canonical_decimal=None,
    ) -> None:
        initial = _canonical_decimal(self, initial_bankroll, "initial_bankroll")
        if initial <= 0:
            raise ValueError("initial virtual bankroll must be positive")
        self.initial_bankroll = initial
        self.balance = self.initial_bankroll
        self.tickets: dict[str, PaperTicket] = {}
        # PaperBook owns the minimum lifecycle witness required to replay bankroll
        # chronology and settlement economics without changing the domain model.
        self._lifecycle: list[_LifecycleEntry] = []
        # Settlement timestamps are causal lifecycle evidence, not mutable ticket
        # decoration. Keeping them in a sidecar preserves the canonical four-field
        # lifecycle tuple used by rollback/state hashes while making timestamp
        # mutation mechanically detectable.
        self._settlement_times: dict[str, str | None] = {}

    @property
    @_serialized_paperbook_operation
    @_guard_paperbook_runtime_authority
    def committed_stake(self) -> Decimal:
        try:
            with localcontext(_paper_decimal_context()) as context:
                total = Decimal("0")
                for ticket in self.tickets.values():
                    if ticket.status is TicketStatus.OPEN:
                        total += ticket.stake
                if context.flags[Inexact]:
                    raise ValueError("PaperBook committed stake loses Decimal precision")
        except DecimalException as exc:
            raise ValueError(
                "PaperBook committed stake arithmetic is not representable"
            ) from exc
        self._require_finite(total, "committed_stake")
        return total

    @classmethod
    def _canonical_decimal_input(cls, value: object, label: str) -> Decimal:
        if type(value) not in {Decimal, str, int, float}:
            raise ValueError(
                f"PaperBook {label} must be an exact built-in Decimal, string, integer or float"
            )
        if type(value) is Decimal:
            parsed = value
        else:
            if type(value) is str and len(value) > _MAX_PAPER_DECIMAL_TEXT_CHARS:
                raise ValueError(
                    f"PaperBook {label} decimal text exceeds the canonical size limit"
                )
            try:
                parsed = Decimal(str(value))
            except (DecimalException, ValueError) as exc:
                raise ValueError(f"PaperBook {label} is not a valid Decimal value") from exc
        cls._require_finite(parsed, label)
        return parsed

    @classmethod
    def _debit_balance(cls, balance: Decimal, amount: Decimal) -> Decimal:
        cls._require_finite(balance, "balance")
        cls._require_finite(amount, "stake")
        if amount <= 0:
            raise ValueError("stake must be positive")
        if amount > balance:
            raise ValueError("insufficient virtual bankroll")
        try:
            with localcontext(_paper_decimal_context()) as context:
                new_balance = balance - amount
                if context.flags[Inexact]:
                    raise ValueError("PaperBook stake debit loses Decimal precision")
        except DecimalException as exc:
            raise ValueError("PaperBook stake debit arithmetic is not representable") from exc
        return new_balance

    @_serialized_paperbook_operation
    @_guard_paperbook_runtime_authority
    @_seal_paperbook_open_transition_authority
    def open_ticket(
        self,
        legs,
        stake,
        reason: str = "",
        placed_at: str | None = None,
        *,
        provider_source_ids: tuple[str, ...] = (),
        provider_accounts: tuple[tuple[str, str], ...] = (),
        bankroll_id: str | None = None,
        currency: str | None = None,
        _opening_authority_record=None,
        _causal_open_advance=None,
        _ticket_id_factory=None,
        _placed_at_now=None,
        _ticket_factory=None,
        _canonical_decimal=None,
        _debit_balance=None,
    ) -> PaperTicket:
        amount = _canonical_decimal(type(self), stake, "stake")
        new_balance = _debit_balance(type(self), self.balance, amount)

        ticket_placed_at = self._validate_placed_at(
            placed_at if placed_at is not None else _placed_at_now()
        )
        self._require_utf8_string(reason, "strategy_reason")
        (
            provider_source_ids,
            provider_accounts,
            bankroll_id,
            currency,
        ) = self._validate_ticket_provenance(
            provider_source_ids,
            provider_accounts,
            bankroll_id,
            currency,
        )
        if type(legs) not in {list, tuple}:
            raise ValueError("ticket legs must be an exact list or tuple")
        ticket_legs = tuple(legs)
        if not ticket_legs:
            raise ValueError("ticket requires at least one leg")
        for leg in ticket_legs:
            self._validate_ticket_leg(leg)
        quote_keys = [leg.quote_key for leg in ticket_legs]
        if len(quote_keys) != len(set(quote_keys)):
            raise ValueError("ticket contains duplicate quote_key leg")
        ticket = _ticket_factory(
            ticket_id=_ticket_id_factory(),
            stake=amount,
            legs=ticket_legs,
            placed_at=ticket_placed_at,
            strategy_reason=reason,
            provider_source_ids=provider_source_ids,
            provider_accounts=provider_accounts,
            bankroll_id=bankroll_id,
            currency=currency,
        )
        _opening_authority_record(self, ticket)
        self.balance = new_balance
        self.tickets[ticket.ticket_id] = ticket
        self._lifecycle.append(("open", ticket.ticket_id, (), ()))
        _causal_open_advance(self, ticket.ticket_id)
        return ticket

    @staticmethod
    def _normalize_resolution_keys(values: object, label: str) -> set[str]:
        if type(values) not in {set, frozenset, list, tuple}:
            raise ValueError(
                f"PaperBook {label} must be an exact built-in collection of quote keys"
            )
        if any(type(value) is not str or not value for value in values):
            raise ValueError(f"PaperBook {label} must contain non-empty string quote keys")
        return set(values)

    @classmethod
    def _settlement_result(
        cls,
        ticket: PaperTicket,
        balance: Decimal,
        winning_quote_keys: set[str],
        void_quote_keys: set[str],
    ) -> tuple[TicketStatus, Decimal, Decimal]:
        cls._require_finite(balance, "balance")
        for leg in ticket.legs:
            cls._validate_ticket_leg(leg, ticket_id=ticket.ticket_id)
        leg_quote_keys = {leg.quote_key for leg in ticket.legs}
        unknown_winners = winning_quote_keys - leg_quote_keys
        unknown_voids = void_quote_keys - leg_quote_keys
        if unknown_winners:
            raise ValueError("PaperBook settlement contains unknown winning quote_key")
        if unknown_voids:
            raise ValueError("PaperBook settlement contains unknown void quote_key")
        if winning_quote_keys & void_quote_keys:
            raise ValueError("PaperBook settlement quote_key cannot be both winning and void")

        effective_legs = tuple(
            leg for leg in ticket.legs if leg.quote_key not in void_quote_keys
        )
        if any(leg.quote_key not in winning_quote_keys for leg in effective_legs):
            return TicketStatus.LOST, Decimal("0"), balance

        status = TicketStatus.VOID if not effective_legs else TicketStatus.WON
        try:
            with localcontext(_paper_decimal_context()) as context:
                effective_odds = Decimal("1")
                for leg in effective_legs:
                    effective_odds *= leg.locked_odds
                payout = ticket.stake if status is TicketStatus.VOID else ticket.stake * effective_odds
                cls._require_finite(payout, f"settlement payout for ticket {ticket.ticket_id}")
                if status is TicketStatus.WON and payout <= ticket.stake:
                    raise ValueError(
                        "PaperBook winning settlement payout must exceed stake after canonical Decimal rounding"
                    )
                new_balance = balance + payout
                cls._require_finite(new_balance, f"balance after settling ticket {ticket.ticket_id}")
                if payout != 0 and new_balance == balance:
                    raise ValueError("PaperBook settlement payout loses all Decimal balance effect")
                if context.flags[Inexact]:
                    raise ValueError("PaperBook settlement arithmetic loses Decimal precision")
        except DecimalException as exc:
            raise ValueError("PaperBook settlement arithmetic is not representable") from exc
        return status, payout, new_balance

    @_serialized_paperbook_operation
    @_guard_paperbook_runtime_authority
    @_seal_paperbook_settle_transition_authority
    def settle(
        self,
        ticket_id: str,
        winning_quote_keys: set[str],
        void_quote_keys: set[str] | None = None,
        *,
        settled_at: str | None = None,
        _causal_settle_advance=None,
        _settlement_result=None,
    ) -> PaperTicket:
        ticket_id = self._require_canonical_text(ticket_id, "settlement ticket_id")
        ticket = self.tickets[ticket_id]
        if ticket.status is not TicketStatus.OPEN:
            raise ValueError("ticket already settled")

        winners = self._normalize_resolution_keys(winning_quote_keys, "winning_quote_keys")
        voids = (
            set()
            if void_quote_keys is None
            else self._normalize_resolution_keys(void_quote_keys, "void_quote_keys")
        )
        settlement_time = (
            None
            if settled_at is None
            else self._validate_settled_at(settled_at, ticket.placed_at)
        )
        status, payout, new_balance = _settlement_result(
            type(self),
            ticket,
            self.balance,
            winners,
            voids,
        )

        ticket.payout = payout
        ticket.status = status
        ticket.settled_at = settlement_time
        self.balance = new_balance
        self._lifecycle.append(
            (
                "settle",
                ticket.ticket_id,
                tuple(sorted(winners)),
                tuple(sorted(voids)),
            )
        )
        self._settlement_times[ticket.ticket_id] = settlement_time
        _causal_settle_advance(
            self,
            ticket.ticket_id,
            tuple(sorted(winners)),
            tuple(sorted(voids)),
            settlement_time,
        )
        return ticket

    def _lifecycle_to_json(self) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = []
        for action, ticket_id, winners, voids in self._lifecycle:
            if action == "open":
                payload.append({"action": "open", "ticket_id": ticket_id})
            else:
                payload.append(
                    {
                        "action": "settle",
                        "ticket_id": ticket_id,
                        "winning_quote_keys": list(winners),
                        "void_quote_keys": list(voids),
                        "settled_at": self._settlement_times[ticket_id],
                    }
                )
        return payload

    @staticmethod
    def _canonical_snapshot_path(path: object) -> Path:
        if type(path) not in {str, type(Path("."))}:
            raise TypeError("PaperBook snapshot path must be exact str or exact Path")
        return Path(path)

    @staticmethod
    def _fsync_snapshot_directory(directory: Path) -> None:
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if directory_flag is None:
            return
        descriptor = os.open(directory, os.O_RDONLY | directory_flag)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _ensure_snapshot_parent_durable(cls, directory: Path) -> None:
        missing: list[Path] = []
        cursor = directory
        while not cursor.exists():
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                break
            cursor = parent

        directory.mkdir(parents=True, exist_ok=True)
        for created in reversed(missing):
            cls._fsync_snapshot_directory(created.parent)

    @_serialized_paperbook_operation
    @_guard_paperbook_runtime_authority
    @_seal_paperbook_save_candidate_authority
    @_seal_paperbook_snapshot_json_publish_authority
    def save(
        self,
        path: str | Path,
        *,
        _opening_candidate_authority=None,
        _causal_candidate_authority=None,
        _canonical_snapshot_decode=None,
        _snapshot_path=None,
        _lifecycle_json=None,
        _ensure_parent_durable=None,
        _fsync_directory=None,
        _json_dump=None,
    ) -> None:
        # Runtime visible-state + hidden-authority validation is performed once
        # by the closure-captured guard before this body executes.
        destination = _snapshot_path(type(self), path)
        raw = {
            "schema_version": _PAPER_SNAPSHOT_SCHEMA_VERSION,
            "initial_bankroll": str(self.initial_bankroll),
            "balance": str(self.balance),
            "tickets": [
                {
                    "ticket_id": t.ticket_id,
                    "stake": str(t.stake),
                    "placed_at": t.placed_at,
                    "settled_at": t.settled_at,
                    "status": t.status.value,
                    "payout": str(t.payout),
                    "strategy_reason": t.strategy_reason,
                    "provider_source_ids": list(t.provider_source_ids),
                    "provider_accounts": [
                        {"source_id": source_id, "account_id": account_id}
                        for source_id, account_id in t.provider_accounts
                    ],
                    "bankroll_id": t.bankroll_id,
                    "currency": t.currency,
                    "legs": [
                        {
                            "event_id": leg.event_id,
                            "market_id": leg.market_id,
                            "selection_id": leg.selection_id,
                            "locked_odds": str(leg.locked_odds),
                            "sport": leg.sport,
                            "exchange_side": leg.exchange_side,
                        }
                        for leg in t.legs
                    ],
                }
                for t in self.tickets.values()
            ],
            "lifecycle": _lifecycle_json(type(self), self),
        }

        # The raw snapshot is detached from the mutable live object. Validate
        # that exact candidate against product-issued opening commitments before
        # any durable replacement, closing coherent mutation during collection.
        candidate = _canonical_snapshot_decode(type(self), raw)
        _opening_candidate_authority(self, candidate)
        _causal_candidate_authority(self, candidate)

        # Rejected candidates must not publish filesystem state.
        _ensure_parent_durable(type(self), destination.parent)

        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                _json_dump(raw, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
            _fsync_directory(type(self), destination.parent)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _require_finite(value: object, label: str) -> None:
        if type(value) is not Decimal or not value.is_finite():
            raise ValueError(f"PaperBook snapshot contains non-finite {label}")

    @staticmethod
    def _require_utf8_string(value: object, label: str) -> str:
        if type(value) is not str:
            raise ValueError(f"PaperBook {label} must be a string")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError(f"PaperBook {label} must be valid UTF-8 text") from exc
        return value

    @classmethod
    def _require_canonical_text(
        cls,
        value: object,
        label: str,
        *,
        forbid_quote_key_delimiter: bool = False,
    ) -> str:
        text = cls._require_utf8_string(value, label)
        if not text or text.strip() != text:
            raise ValueError(f"PaperBook {label} must be a non-empty trimmed string")
        if forbid_quote_key_delimiter and "|" in text:
            raise ValueError(f"PaperBook {label} must not contain quote-key delimiter '|'")
        return text

    @classmethod
    def _validate_ticket_provenance(
        cls,
        provider_source_ids: object,
        provider_accounts: object,
        bankroll_id: object,
        currency: object,
    ) -> tuple[
        tuple[str, ...],
        tuple[tuple[str, str], ...],
        str | None,
        str | None,
    ]:
        if type(provider_source_ids) is not tuple:
            raise ValueError("PaperBook provider_source_ids must be a canonical tuple")
        canonical_sources = tuple(
            cls._require_canonical_text(source_id, "provider_source_id")
            for source_id in provider_source_ids
        )
        if (
            canonical_sources != tuple(sorted(canonical_sources))
            or len(canonical_sources) != len(set(canonical_sources))
        ):
            raise ValueError("PaperBook provider_source_ids must be sorted and unique")

        if type(provider_accounts) is not tuple:
            raise ValueError("PaperBook provider_accounts must be a canonical tuple")
        account_bindings: list[tuple[str, str]] = []
        for binding in provider_accounts:
            if type(binding) is not tuple or len(binding) != 2:
                raise ValueError(
                    "PaperBook provider_accounts must contain (source_id, account_id) tuples"
                )
            source_id, account_id = binding
            account_bindings.append(
                (
                    cls._require_canonical_text(source_id, "provider account source_id"),
                    cls._require_canonical_text(account_id, "provider account_id"),
                )
            )
        canonical_accounts = tuple(account_bindings)
        if (
            canonical_accounts != tuple(sorted(canonical_accounts))
            or len(canonical_accounts) != len(set(canonical_accounts))
        ):
            raise ValueError("PaperBook provider_accounts must be sorted and unique")
        account_sources = tuple(source_id for source_id, _ in canonical_accounts)
        if len(account_sources) != len(set(account_sources)):
            raise ValueError(
                "PaperBook provider_accounts may bind at most one account_id per provider source"
            )
        if canonical_accounts and frozenset(account_sources) != frozenset(canonical_sources):
            raise ValueError(
                "PaperBook provider_accounts must cover provider_source_ids exactly"
            )

        if (bankroll_id is None) != (currency is None):
            raise ValueError(
                "PaperBook bankroll_id and currency provenance must be supplied together"
            )
        if bankroll_id is None:
            return canonical_sources, canonical_accounts, None, None

        canonical_bankroll = cls._require_canonical_text(bankroll_id, "bankroll_id")
        canonical_currency = cls._require_canonical_text(currency, "currency")
        if (
            len(canonical_currency) != 3
            or not canonical_currency.isascii()
            or not canonical_currency.isalpha()
            or canonical_currency != canonical_currency.upper()
        ):
            raise ValueError(
                "PaperBook currency provenance must be a three-letter uppercase ASCII code"
            )
        return canonical_sources, canonical_accounts, canonical_bankroll, canonical_currency

    @classmethod
    def _validate_timestamp(cls, value: object, label: str) -> str:
        message = f"PaperBook {label} must be a non-empty trimmed timezone-aware ISO timestamp"
        try:
            text = cls._require_utf8_string(value, label)
        except ValueError as exc:
            raise ValueError(message) from exc
        if not text or text.strip() != text:
            raise ValueError(message)
        try:
            parse_iso_timestamp(text)
        except ValueError as exc:
            raise ValueError(message) from exc
        return text

    @classmethod
    def _validate_placed_at(cls, value: object, *, snapshot: bool = False) -> str:
        label = "snapshot placed_at" if snapshot else "placed_at"
        return cls._validate_timestamp(value, label)

    @classmethod
    def _validate_settled_at(
        cls,
        value: object,
        placed_at: object,
        *,
        snapshot: bool = False,
    ) -> str:
        label = "snapshot settled_at" if snapshot else "settled_at"
        settled_text = cls._validate_timestamp(value, label)
        placed_text = cls._validate_placed_at(placed_at, snapshot=snapshot)
        if parse_iso_timestamp(settled_text) < parse_iso_timestamp(placed_text):
            raise ValueError("PaperBook settled_at must not precede placed_at")
        return settled_text

    @classmethod
    def _validate_ticket_leg(cls, leg: object, *, ticket_id: str | None = None) -> TicketLeg:
        if type(leg) is not TicketLeg:
            raise ValueError("PaperBook ticket legs must be canonical TicketLeg values")
        suffix = f" for ticket {ticket_id}" if ticket_id is not None else ""
        cls._require_canonical_text(
            leg.event_id,
            f"event_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        cls._require_canonical_text(
            leg.market_id,
            f"market_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        cls._require_canonical_text(
            leg.selection_id,
            f"selection_id{suffix}",
            forbid_quote_key_delimiter=True,
        )
        if leg.sport is not None:
            sport = cls._require_canonical_text(
                leg.sport,
                f"sport{suffix}",
                forbid_quote_key_delimiter=True,
            )
            if sport != sport.lower() or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in sport
            ):
                raise ValueError(
                    "PaperBook ticket sport must be a lowercase canonical sport identity"
                )
        if leg.exchange_side is not None:
            exchange_side = cls._require_canonical_text(
                leg.exchange_side,
                f"exchange_side{suffix}",
                forbid_quote_key_delimiter=True,
            )
            if exchange_side not in {"back", "lay"}:
                raise ValueError(
                    "PaperBook ticket exchange_side must be canonical 'back' or 'lay'"
                )
            if exchange_side == "lay":
                raise ValueError(
                    "PaperBook LAY economic materialization is not supported"
                )
        cls._require_finite(leg.locked_odds, f"locked_odds{suffix}")
        if leg.locked_odds <= 1:
            raise ValueError("PaperBook snapshot decimal odds must be greater than 1")
        return leg

    @classmethod
    def _validate_lifecycle_entry(cls, entry: object) -> _LifecycleEntry:
        if type(entry) is not tuple or len(entry) != 4:
            raise ValueError("PaperBook lifecycle entries must be canonical tuples")
        action, ticket_id, winners, voids = entry
        if type(action) is not str or action not in {"open", "settle"}:
            raise ValueError(
                "PaperBook lifecycle action must be canonical open or settle text"
            )
        cls._require_canonical_text(ticket_id, "lifecycle ticket_id")
        if type(winners) is not tuple or type(voids) is not tuple:
            raise ValueError("PaperBook lifecycle settlement keys must be canonical tuples")
        for values, label in ((winners, "winning_quote_keys"), (voids, "void_quote_keys")):
            for value in values:
                cls._require_canonical_text(value, f"lifecycle {label}")
            if values != tuple(sorted(values)) or len(values) != len(set(values)):
                raise ValueError(f"PaperBook lifecycle {label} must be sorted and unique")
        if action == "open" and (winners or voids):
            raise ValueError("PaperBook lifecycle open action cannot contain settlement keys")
        return action, ticket_id, winners, voids

    @classmethod
    def _validate_lifecycle_reachability(cls, book: "PaperBook") -> None:
        if type(book._lifecycle) is not list:
            raise ValueError("PaperBook lifecycle must be a canonical list")
        if type(book._settlement_times) is not dict:
            raise ValueError("PaperBook settlement-time witness must be a canonical mapping")
        if any(type(ticket_id) is not str for ticket_id in book._settlement_times):
            raise ValueError(
                "PaperBook settlement-time witness keys must be canonical strings"
            )

        replay_balance = book.initial_bankroll
        opened: set[str] = set()
        settled: set[str] = set()
        open_order: list[str] = []

        for raw_entry in book._lifecycle:
            action, ticket_id, winners_raw, voids_raw = cls._validate_lifecycle_entry(
                raw_entry
            )
            ticket = book.tickets.get(ticket_id)
            if ticket is None:
                raise ValueError("PaperBook lifecycle references unknown ticket_id")

            if action == "open":
                if ticket_id in opened:
                    raise ValueError("PaperBook lifecycle opens a ticket more than once")
                try:
                    replay_balance = cls._debit_balance(replay_balance, ticket.stake)
                except ValueError as exc:
                    raise ValueError(
                        f"PaperBook lifecycle stake for ticket {ticket_id} was not affordable"
                    ) from exc
                opened.add(ticket_id)
                open_order.append(ticket_id)
                continue

            if ticket_id not in opened:
                raise ValueError("PaperBook lifecycle settles a ticket before opening it")
            if ticket_id in settled:
                raise ValueError("PaperBook lifecycle settles a ticket more than once")
            if ticket_id not in book._settlement_times:
                raise ValueError(
                    f"PaperBook ticket {ticket_id} settlement is missing timestamp provenance witness"
                )
            settlement_time = book._settlement_times[ticket_id]
            if ticket.settled_at != settlement_time:
                raise ValueError(
                    f"PaperBook ticket {ticket_id} settled_at is inconsistent with lifecycle provenance"
                )
            if settlement_time is not None:
                cls._validate_settled_at(
                    settlement_time,
                    ticket.placed_at,
                    snapshot=True,
                )
            winners = set(winners_raw)
            voids = set(voids_raw)
            status, payout, replay_balance = cls._settlement_result(
                ticket,
                replay_balance,
                winners,
                voids,
            )
            if ticket.status is not status or ticket.payout != payout:
                raise ValueError(
                    f"PaperBook ticket {ticket_id} state is inconsistent with lifecycle settlement witness"
                )
            settled.add(ticket_id)

        if tuple(open_order) != tuple(book.tickets):
            raise ValueError("PaperBook lifecycle open order must match canonical ticket order")
        for ticket_id, ticket in book.tickets.items():
            if ticket_id not in opened:
                raise ValueError("PaperBook lifecycle is missing ticket open action")
            if ticket_id not in settled and ticket.status is not TicketStatus.OPEN:
                raise ValueError(
                    f"PaperBook ticket {ticket_id} settled state is missing lifecycle provenance"
                )
            if ticket_id in settled and ticket.status is TicketStatus.OPEN:
                raise ValueError(
                    f"PaperBook ticket {ticket_id} open state conflicts with lifecycle settlement witness"
                )
        if set(book._settlement_times) != settled:
            raise ValueError(
                "PaperBook settlement-time witness must match settled lifecycle tickets exactly"
            )
        if replay_balance != book.balance:
            raise ValueError(
                "PaperBook snapshot balance is inconsistent with lifecycle-replayed ticket economics"
            )

    @classmethod
    def _validate_loaded_state(cls, book: "PaperBook") -> None:
        cls._require_finite(book.initial_bankroll, "initial_bankroll")
        cls._require_finite(book.balance, "balance")
        if book.initial_bankroll <= 0:
            raise ValueError("PaperBook snapshot initial_bankroll must be positive")
        if book.balance < 0:
            raise ValueError("PaperBook snapshot balance cannot be negative")
        if type(book.tickets) is not dict:
            raise ValueError("PaperBook tickets must be a canonical ticket mapping")

        for ticket_key, ticket in book.tickets.items():
            cls._require_canonical_text(ticket_key, "ticket mapping key")
            if type(ticket) is not PaperTicket:
                raise ValueError("PaperBook tickets must contain canonical PaperTicket values")
            cls._require_canonical_text(ticket.ticket_id, "ticket_id")
            if ticket_key != ticket.ticket_id:
                raise ValueError("PaperBook ticket mapping key must match ticket_id")
            cls._validate_placed_at(ticket.placed_at, snapshot=True)
            if ticket.settled_at is not None:
                cls._validate_settled_at(
                    ticket.settled_at,
                    ticket.placed_at,
                    snapshot=True,
                )
            if ticket.status is TicketStatus.OPEN and ticket.settled_at is not None:
                raise ValueError("PaperBook snapshot open ticket cannot have settled_at")
            cls._require_utf8_string(ticket.strategy_reason, "snapshot strategy_reason")
            cls._validate_ticket_provenance(
                ticket.provider_source_ids,
                ticket.provider_accounts,
                ticket.bankroll_id,
                ticket.currency,
            )
            if type(ticket.status) is not TicketStatus:
                raise ValueError("PaperBook snapshot ticket status must be canonical TicketStatus")
            cls._require_finite(ticket.stake, f"stake for ticket {ticket.ticket_id}")
            cls._require_finite(ticket.payout, f"payout for ticket {ticket.ticket_id}")
            if ticket.stake <= 0:
                raise ValueError("PaperBook snapshot ticket stake must be positive")
            if ticket.payout < 0:
                raise ValueError("PaperBook snapshot ticket payout cannot be negative")
            if type(ticket.legs) is not tuple or not ticket.legs:
                raise ValueError("PaperBook snapshot ticket requires a canonical non-empty leg tuple")
            for leg in ticket.legs:
                cls._validate_ticket_leg(leg, ticket_id=ticket.ticket_id)
            quote_keys = [leg.quote_key for leg in ticket.legs]
            if len(quote_keys) != len(set(quote_keys)):
                raise ValueError("PaperBook snapshot ticket contains duplicate quote_key leg")

            if ticket.status in {TicketStatus.OPEN, TicketStatus.LOST} and ticket.payout != 0:
                raise ValueError("PaperBook snapshot open/lost ticket payout must be zero")
            if ticket.status is TicketStatus.VOID and ticket.payout != ticket.stake:
                raise ValueError("PaperBook snapshot void ticket payout must equal stake")
            if ticket.status is TicketStatus.WON and ticket.payout <= ticket.stake:
                raise ValueError("PaperBook snapshot won ticket payout must exceed stake")

        cls._validate_lifecycle_reachability(book)

    @classmethod
    def _parse_lifecycle_key_list(cls, value: object, label: str) -> tuple[str, ...]:
        if type(value) is not list:
            raise ValueError(f"PaperBook snapshot lifecycle {label} must be a list")
        for item in value:
            cls._require_canonical_text(item, f"snapshot lifecycle {label}")
        normalized = tuple(value)
        if normalized != tuple(sorted(normalized)) or len(normalized) != len(set(normalized)):
            raise ValueError(
                f"PaperBook snapshot lifecycle {label} must be sorted and unique"
            )
        return normalized

    @classmethod
    def _parse_lifecycle(
        cls,
        value: object,
        schema_version: int,
    ) -> tuple[list[_LifecycleEntry], dict[str, str | None]]:
        if type(value) is not list:
            raise ValueError("PaperBook snapshot lifecycle must be a list")
        entries: list[_LifecycleEntry] = []
        settlement_times: dict[str, str | None] = {}
        for item in value:
            if type(item) is not dict:
                raise ValueError("PaperBook snapshot lifecycle entry must be an object")
            action = item.get("action")
            ticket_id = item.get("ticket_id")
            if type(action) is not str:
                raise ValueError(
                    "PaperBook snapshot lifecycle action must be canonical text"
                )
            if action == "open":
                if set(item) != {"action", "ticket_id"}:
                    raise ValueError("PaperBook snapshot open lifecycle entry has unexpected fields")
                entry: _LifecycleEntry = ("open", ticket_id, (), ())
            elif action == "settle":
                expected_fields = {
                    "action",
                    "ticket_id",
                    "winning_quote_keys",
                    "void_quote_keys",
                }
                if schema_version >= 5:
                    expected_fields.add("settled_at")
                if set(item) != expected_fields:
                    raise ValueError("PaperBook snapshot settle lifecycle entry has unexpected fields")
                settled_at = item.get("settled_at") if schema_version >= 5 else None
                if settled_at is not None:
                    cls._validate_timestamp(
                        settled_at,
                        "snapshot lifecycle settled_at",
                    )
                entry = (
                    "settle",
                    ticket_id,
                    cls._parse_lifecycle_key_list(
                        item["winning_quote_keys"], "winning_quote_keys"
                    ),
                    cls._parse_lifecycle_key_list(
                        item["void_quote_keys"], "void_quote_keys"
                    ),
                )
            else:
                raise ValueError("PaperBook snapshot lifecycle action must be open or settle")
            canonical_entry = cls._validate_lifecycle_entry(entry)
            entries.append(canonical_entry)
            if action == "settle":
                settlement_times[canonical_entry[1]] = settled_at
        return entries, settlement_times

    @classmethod
    def _parse_snapshot_decimal(cls, value: object, label: str) -> Decimal:
        if type(value) is not str or not value or value.strip() != value:
            raise ValueError(
                f"PaperBook snapshot {label} must be a non-empty trimmed decimal string"
            )
        if len(value) > _MAX_PAPER_DECIMAL_TEXT_CHARS:
            raise ValueError(
                f"PaperBook snapshot {label} decimal text exceeds the canonical size limit"
            )
        try:
            parsed = Decimal(value)
        except DecimalException as exc:
            raise ValueError(f"PaperBook snapshot {label} is not a valid Decimal string") from exc
        cls._require_finite(parsed, label)
        return parsed

    @staticmethod
    def _required_snapshot_field(
        mapping: dict[str, object], field: str, context: str
    ) -> object:
        if field not in mapping:
            raise ValueError(
                f"PaperBook snapshot {context} is missing required field: {field}"
            )
        return mapping[field]

    @classmethod
    def _parse_snapshot_legs(
        cls,
        value: object,
        ticket_id: str,
        *,
        schema_version: int | None,
    ) -> tuple[TicketLeg, ...]:
        if type(value) is not list:
            raise ValueError(f"PaperBook snapshot legs for ticket {ticket_id} must be a list")
        legs: list[TicketLeg] = []
        for index, raw_leg in enumerate(value):
            if type(raw_leg) is not dict:
                raise ValueError(
                    f"PaperBook snapshot leg {index} for ticket {ticket_id} must be an object"
                )
            expected_leg_fields = {
                "event_id",
                "market_id",
                "selection_id",
                "locked_odds",
            }
            if schema_version is not None and schema_version >= 6:
                expected_leg_fields.add("sport")
            if schema_version is not None and schema_version >= 7:
                expected_leg_fields.add("exchange_side")
            if set(raw_leg) - expected_leg_fields:
                version_label = (
                    "legacy" if schema_version is None else f"schema {schema_version}"
                )
                raise ValueError(
                    f"PaperBook snapshot {version_label} ticket leg contains unexpected fields"
                )
            sport = (
                cls._required_snapshot_field(raw_leg, "sport", "ticket leg")
                if schema_version is not None and schema_version >= 6
                else None
            )
            exchange_side = (
                cls._required_snapshot_field(raw_leg, "exchange_side", "ticket leg")
                if schema_version is not None and schema_version >= 7
                else None
            )
            if sport is not None:
                cls._require_canonical_text(
                    sport,
                    f"sport for ticket {ticket_id}",
                    forbid_quote_key_delimiter=True,
                )
            legs.append(
                TicketLeg(
                    cls._required_snapshot_field(raw_leg, "event_id", "ticket leg"),
                    cls._required_snapshot_field(raw_leg, "market_id", "ticket leg"),
                    cls._required_snapshot_field(raw_leg, "selection_id", "ticket leg"),
                    cls._parse_snapshot_decimal(
                        cls._required_snapshot_field(raw_leg, "locked_odds", "ticket leg"),
                        f"locked_odds for ticket {ticket_id}",
                    ),
                    sport=sport,
                    exchange_side=exchange_side,
                )
            )
        return tuple(legs)

    @classmethod
    def _parse_snapshot_provider_accounts(
        cls, value: object, ticket_id: str
    ) -> tuple[tuple[str, str], ...]:
        if type(value) is not list:
            raise ValueError(
                f"PaperBook snapshot provider_accounts for ticket {ticket_id} must be a list"
            )
        bindings: list[tuple[str, str]] = []
        for index, raw_binding in enumerate(value):
            if type(raw_binding) is not dict or set(raw_binding) != {"source_id", "account_id"}:
                raise ValueError(
                    "PaperBook snapshot provider account binding "
                    f"{index} for ticket {ticket_id} must contain exactly source_id and account_id"
                )
            bindings.append(
                (
                    cls._require_canonical_text(
                        raw_binding["source_id"], "snapshot provider account source_id"
                    ),
                    cls._require_canonical_text(
                        raw_binding["account_id"], "snapshot provider account_id"
                    ),
                )
            )
        return tuple(bindings)

    @staticmethod
    def _parse_snapshot_status(value: object, ticket_id: str) -> TicketStatus:
        if type(value) is not str:
            raise ValueError(
                f"PaperBook snapshot status for ticket {ticket_id} must be a canonical string"
            )
        try:
            return TicketStatus(value)
        except ValueError as exc:
            raise ValueError(
                f"PaperBook snapshot status for ticket {ticket_id} is invalid"
            ) from exc

    @classmethod
    @_seal_paperbook_snapshot_decode_authority
    def _from_raw_snapshot(
        cls,
        raw: object,
        *,
        _snapshot_authority_revoke=None,
    ) -> "PaperBook":
        if type(raw) is not dict:
            raise ValueError("PaperBook snapshot root must be an object")
        schema_version = raw.get("schema_version", _SCHEMA_MISSING)
        is_legacy = schema_version is _SCHEMA_MISSING
        if not is_legacy and (
            type(schema_version) is not int
            or schema_version not in _SUPPORTED_PAPER_SNAPSHOT_SCHEMA_VERSIONS
        ):
            raise ValueError("unsupported PaperBook snapshot schema_version")
        expected_root_fields = (
            {"initial_bankroll", "balance", "tickets"}
            if is_legacy
            else {
                "schema_version",
                "initial_bankroll",
                "balance",
                "tickets",
                "lifecycle",
            }
        )
        if set(raw) - expected_root_fields:
            version_label = "legacy" if is_legacy else f"schema {schema_version}"
            raise ValueError(
                f"PaperBook snapshot {version_label} root contains unexpected fields"
            )

        initial_bankroll = cls._parse_snapshot_decimal(
            cls._required_snapshot_field(raw, "initial_bankroll", "root"),
            "initial_bankroll",
        )
        book = cls(initial_bankroll)
        book.balance = cls._parse_snapshot_decimal(
            cls._required_snapshot_field(raw, "balance", "root"),
            "balance",
        )
        tickets_raw = cls._required_snapshot_field(raw, "tickets", "root")
        if type(tickets_raw) is not list:
            raise ValueError("PaperBook snapshot tickets must be a list")
        seen_ticket_ids: set[str] = set()
        for item in tickets_raw:
            if type(item) is not dict:
                raise ValueError("PaperBook snapshot ticket must be an object")
            expected_ticket_fields = {
                "ticket_id",
                "stake",
                "legs",
                "placed_at",
                "status",
                "payout",
                "strategy_reason",
            }
            if not is_legacy and schema_version >= 3:
                expected_ticket_fields.update(
                    {"provider_source_ids", "bankroll_id", "currency"}
                )
            if not is_legacy and schema_version >= 4:
                expected_ticket_fields.add("provider_accounts")
            if not is_legacy and schema_version >= 5:
                expected_ticket_fields.add("settled_at")
            if set(item) - expected_ticket_fields:
                version_label = "legacy" if is_legacy else f"schema {schema_version}"
                raise ValueError(
                    f"PaperBook snapshot {version_label} ticket contains unexpected fields"
                )
            ticket_id = cls._require_canonical_text(
                cls._required_snapshot_field(item, "ticket_id", "ticket"),
                "snapshot ticket_id",
            )
            if ticket_id in seen_ticket_ids:
                raise ValueError("PaperBook snapshot contains duplicate ticket_id")
            seen_ticket_ids.add(ticket_id)
            if schema_version in {3, 4, 5, 6, 7}:
                provider_source_ids_raw = cls._required_snapshot_field(
                    item, "provider_source_ids", f"ticket {ticket_id}"
                )
                if type(provider_source_ids_raw) is not list:
                    raise ValueError(
                        f"PaperBook snapshot provider_source_ids for ticket {ticket_id} must be a list"
                    )
                provider_source_ids = tuple(provider_source_ids_raw)
                provider_accounts = (
                    cls._parse_snapshot_provider_accounts(
                        cls._required_snapshot_field(
                            item, "provider_accounts", f"ticket {ticket_id}"
                        ),
                        ticket_id,
                    )
                    if schema_version in {4, 5, 6, 7}
                    else ()
                )
                bankroll_id = cls._required_snapshot_field(
                    item, "bankroll_id", f"ticket {ticket_id}"
                )
                currency = cls._required_snapshot_field(
                    item, "currency", f"ticket {ticket_id}"
                )
            else:
                provider_source_ids = ()
                provider_accounts = ()
                bankroll_id = None
                currency = None

            ticket = PaperTicket(
                ticket_id=ticket_id,
                stake=cls._parse_snapshot_decimal(
                    cls._required_snapshot_field(item, "stake", f"ticket {ticket_id}"),
                    f"stake for ticket {ticket_id}",
                ),
                legs=cls._parse_snapshot_legs(
                    cls._required_snapshot_field(item, "legs", f"ticket {ticket_id}"),
                    ticket_id,
                    schema_version=None if is_legacy else schema_version,
                ),
                placed_at=cls._required_snapshot_field(
                    item, "placed_at", f"ticket {ticket_id}"
                ),
                settled_at=(
                    cls._required_snapshot_field(
                        item, "settled_at", f"ticket {ticket_id}"
                    )
                    if schema_version in {5, 6, 7}
                    else None
                ),
                status=cls._parse_snapshot_status(
                    cls._required_snapshot_field(item, "status", f"ticket {ticket_id}"),
                    ticket_id,
                ),
                payout=cls._parse_snapshot_decimal(
                    cls._required_snapshot_field(item, "payout", f"ticket {ticket_id}"),
                    f"payout for ticket {ticket_id}",
                ),
                strategy_reason=item.get("strategy_reason", ""),
                provider_source_ids=provider_source_ids,
                provider_accounts=provider_accounts,
                bankroll_id=bankroll_id,
                currency=currency,
            )
            book.tickets[ticket.ticket_id] = ticket

        if is_legacy:
            # Legacy snapshots did not persist settlement chronology or resolution
            # witnesses. Open-only books are still exactly replayable from ticket
            # insertion order. Settled legacy books fail closed later in lifecycle
            # validation after all older structural/status invariants have run.
            book._lifecycle = [
                ("open", ticket_id, (), ())
                for ticket_id in book.tickets
            ]
            book._settlement_times = {}
        else:
            if "lifecycle" not in raw:
                raise ValueError(
                    f"PaperBook snapshot schema {schema_version} requires lifecycle provenance"
                )
            book._lifecycle, book._settlement_times = cls._parse_lifecycle(
                raw["lifecycle"],
                schema_version,
            )

        cls._validate_loaded_state(book)
        # Decoding arbitrary bytes proves structure only. It must not mint the
        # product-issued opening authority needed for economic mutation/readout.
        _snapshot_authority_revoke(book)
        return book

    @classmethod
    @_seal_paperbook_json_decode_authority
    def load_bytes(
        cls,
        payload: bytes,
        *,
        _json_decode=None,
        _raw_snapshot_decode=None,
    ) -> "PaperBook":
        if type(payload) is not bytes:
            raise TypeError("PaperBook.load_bytes payload must be canonical bytes")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("PaperBook snapshot must be valid UTF-8") from exc
        try:
            raw = _json_decode(text)
        except RecursionError as exc:
            raise ValueError("PaperBook snapshot JSON nesting is too deep") from exc
        return _raw_snapshot_decode(cls, raw)

    @classmethod
    @_seal_paperbook_snapshot_install_authority
    def load(
        cls,
        path: str | Path,
        *,
        _snapshot_authority_install=None,
        _canonical_load_bytes=None,
        _snapshot_path=None,
    ) -> "PaperBook":
        destination = _snapshot_path(cls, path)
        book = _canonical_load_bytes(cls, destination.read_bytes())
        _snapshot_authority_install(book)
        return book


# Install exact class authority only after PaperBook exists. Constructor/runtime
# guards and registries capture the require-function closure above, so subclasses
# cannot redirect cls/self helper dispatch into attacker-controlled validation.
_install_paperbook_type_authority(PaperBook)
del _install_paperbook_type_authority

_install_paperbook_constructor_decimal_authority(PaperBook)
del _install_paperbook_constructor_decimal_authority

_install_paperbook_economic_helper_dispatch_authority(PaperBook)
del _install_paperbook_economic_helper_dispatch_authority

_install_paperbook_snapshot_entry_dispatch_authority(PaperBook)
del _install_paperbook_snapshot_entry_dispatch_authority

# Install the exact classmethod implementation only after PaperBook exists.
# Public runtime guards already capture the require-function closure above;
# later class/module rebinding therefore cannot replace visible-state validation.
_install_paperbook_visible_state_authority(
    PaperBook.__dict__["_validate_loaded_state"].__func__
)
del _install_paperbook_visible_state_authority
