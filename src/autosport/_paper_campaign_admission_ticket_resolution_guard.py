"""Extend #708's executable admission seal to canonical PaperBook ticket reads.

This is not a second source of PAPER authority.  It closes the remaining mutable
Python-dispatch seam around the already-canonical PaperBook ticket consumed by the
existing admission coordinator.  The public ``admit`` path, ``_ticket`` bridge,
``_execution_ticket`` resolver, canonical PaperBook loader, and construction-time
``paper_book_path`` are closure-pinned together.  Caller class/instance/module
rebinding therefore fails before it can mint an in-memory replacement ticket.
"""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from weakref import WeakKeyDictionary

from . import paper_campaign_admission as _admission_module
from .domain import PaperTicket
from .paper import PaperBook
from .paper_campaign_admission import (
    PaperCampaignAdmissionCoordinator,
    PaperCampaignAdmissionError,
)


_GUARD_MARKER = "__autosport_exact_admission_ticket_resolution_guard_v1__"
_SEAL_MARKER = "autosport.paper_campaign_admission.ticket_resolution_guard.seal.v1"


def _sealed_tuple_from_callable(candidate: object):
    closure = getattr(candidate, "__closure__", None)
    if not closure:
        return None
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if type(value) is tuple and len(value) == 12 and value[0] == _SEAL_MARKER:
            return value
    return None


def _initial_seal():
    return (
        _SEAL_MARKER,
        PaperCampaignAdmissionCoordinator,
        PaperBook,
        PaperBook.__dict__.get("load"),
        PaperBook.load,
        PaperTicket,
        PaperCampaignAdmissionCoordinator.__init__,
        PaperCampaignAdmissionCoordinator._execution_ticket,
        PaperCampaignAdmissionCoordinator._ticket,
        PaperCampaignAdmissionCoordinator.admit,
        WeakKeyDictionary(),
        RLock(),
    )


def _build_guard(seal):
    seal_marker = seal[0]
    coordinator_cls = seal[1]
    paper_book_cls = seal[2]
    paper_book_load_descriptor = seal[3]
    paper_book_load = seal[4]
    paper_ticket_cls = seal[5]
    original_init = seal[6]
    original_execution_ticket = seal[7]
    original_ticket = seal[8]
    original_admit = seal[9]
    path_bindings = seal[10]
    path_bindings_lock = seal[11]
    path_cls = Path

    def reject_instance_shadow(value: object, method_name: str) -> None:
        try:
            namespace = object.__getattribute__(value, "__dict__")
        except (AttributeError, TypeError):
            return
        if type(namespace) is dict and method_name in namespace:
            raise PaperCampaignAdmissionError(
                f"PAPER admission {method_name} authority is shadowed on the exact instance"
            )

    def binding_for(self: PaperCampaignAdmissionCoordinator) -> Path:
        with path_bindings_lock:
            path = path_bindings.get(self)
        if path is None:
            raise PaperCampaignAdmissionError(
                "canonical PaperBook path binding is unavailable"
            )
        return path

    def assert_loader_surface() -> None:
        if _admission_module.PaperBook is not paper_book_cls:
            raise PaperCampaignAdmissionError(
                "canonical PaperBook class changed after admission guard installation"
            )
        if paper_book_cls.__dict__.get("load") is not paper_book_load_descriptor:
            raise PaperCampaignAdmissionError(
                "canonical PaperBook loader changed after admission guard installation"
            )

    def assert_path_binding(self: PaperCampaignAdmissionCoordinator) -> Path:
        expected = binding_for(self)
        try:
            raw = object.__getattribute__(self, "paper_book_path")
        except (AttributeError, TypeError) as exc:
            raise PaperCampaignAdmissionError(
                "canonical PaperBook path authority is unavailable"
            ) from exc
        if type(raw) is not path_cls or raw.resolve(strict=False) != expected:
            raise PaperCampaignAdmissionError(
                "canonical PaperBook path changed after admission construction"
            )
        return expected

    def assert_entry_points(self: PaperCampaignAdmissionCoordinator) -> None:
        if seal[0] != seal_marker:
            raise RuntimeError("PAPER admission ticket-resolution seal changed")
        if coordinator_cls.__init__ is not guarded_init:
            raise PaperCampaignAdmissionError(
                "PAPER admission construction authority changed after guard installation"
            )
        if coordinator_cls._execution_ticket is not guarded_execution_ticket:
            raise PaperCampaignAdmissionError(
                "PAPER admission execution-ticket authority changed after guard installation"
            )
        if coordinator_cls._ticket is not guarded_ticket:
            raise PaperCampaignAdmissionError(
                "PAPER admission ticket bridge changed after guard installation"
            )
        if coordinator_cls.admit is not guarded_admit:
            raise PaperCampaignAdmissionError(
                "PAPER admission public entry point changed after guard installation"
            )
        for name in ("admit", "_execution_ticket", "_ticket"):
            reject_instance_shadow(self, name)
        assert_loader_surface()
        assert_path_binding(self)

    def canonical_ticket(self: PaperCampaignAdmissionCoordinator, binding) -> PaperTicket:
        path = assert_path_binding(self)
        try:
            book = paper_book_load(path)
        except (OSError, TypeError, ValueError) as exc:
            raise PaperCampaignAdmissionError(
                "canonical execution PaperBook is unavailable"
            ) from exc
        attempt_id = getattr(binding, "attempt_id", None)
        ticket_id = getattr(binding, "ticket_id", None)
        if type(attempt_id) is not str or not attempt_id or type(ticket_id) is not str or not ticket_id:
            raise PaperCampaignAdmissionError("PAPER execution ticket binding is invalid")
        marker = f"paper_execution_attempt_id={attempt_id}"
        marker_matches = [
            ticket for ticket in book.tickets.values() if marker in ticket.strategy_reason
        ]
        ticket = book.tickets.get(ticket_id)
        if (
            type(ticket) is not paper_ticket_cls
            or len(marker_matches) != 1
            or marker_matches[0].ticket_id != ticket_id
        ):
            raise PaperCampaignAdmissionError(
                "execution attempt must resolve to exactly one canonical PaperTicket"
            )
        return ticket

    def guarded_init(
        self: PaperCampaignAdmissionCoordinator,
        state_path,
        *,
        paper_book_path,
        decision_ledger,
        runtime,
        execution_ledger,
    ) -> None:
        original_init(
            self,
            state_path,
            paper_book_path=paper_book_path,
            decision_ledger=decision_ledger,
            runtime=runtime,
            execution_ledger=execution_ledger,
        )
        try:
            raw = object.__getattribute__(self, "paper_book_path")
        except (AttributeError, TypeError) as exc:
            raise PaperCampaignAdmissionError(
                "canonical PaperBook path authority is unavailable"
            ) from exc
        if type(raw) is not path_cls:
            raise PaperCampaignAdmissionError("canonical PaperBook path must be pathlib.Path")
        expected = path_cls(paper_book_path).resolve(strict=False)
        if raw.resolve(strict=False) != expected:
            raise PaperCampaignAdmissionError(
                "canonical PaperBook path changed during admission construction"
            )
        with path_bindings_lock:
            path_bindings[self] = expected
        assert_entry_points(self)

    def guarded_execution_ticket(self: PaperCampaignAdmissionCoordinator, binding):
        assert_entry_points(self)
        expected_ticket = canonical_ticket(self, binding)
        result = original_execution_ticket(self, binding)
        assert_entry_points(self)
        if type(result) is not paper_ticket_cls or result != expected_ticket:
            raise PaperCampaignAdmissionError(
                "PAPER execution ticket does not match canonical durable PaperBook bytes"
            )
        return result

    def guarded_ticket(self: PaperCampaignAdmissionCoordinator, *args, **kwargs):
        assert_entry_points(self)
        result = original_ticket(self, *args, **kwargs)
        assert_entry_points(self)
        return result

    def guarded_admit(self: PaperCampaignAdmissionCoordinator, *args, **kwargs):
        assert_entry_points(self)
        result = original_admit(self, *args, **kwargs)
        assert_entry_points(self)
        return result

    return guarded_init, guarded_execution_ticket, guarded_ticket, guarded_admit


def _install() -> None:
    coordinator_cls = PaperCampaignAdmissionCoordinator
    marker = bool(getattr(coordinator_cls, _GUARD_MARKER, False))
    seal = _sealed_tuple_from_callable(coordinator_cls.admit)

    if marker:
        if seal is None:
            raise RuntimeError("PAPER admission ticket-resolution seal is unavailable")
        for candidate in (
            coordinator_cls.__init__,
            coordinator_cls._execution_ticket,
            coordinator_cls._ticket,
            coordinator_cls.admit,
        ):
            if _sealed_tuple_from_callable(candidate) is not seal:
                raise RuntimeError(
                    "PAPER admission ticket-resolution entry point changed after installation"
                )
        return

    if seal is not None:
        raise RuntimeError("unexpected PAPER admission ticket-resolution seal without marker")
    seal = _initial_seal()
    guarded_init, guarded_execution_ticket, guarded_ticket, guarded_admit = _build_guard(seal)
    coordinator_cls.__init__ = guarded_init
    coordinator_cls._execution_ticket = guarded_execution_ticket
    coordinator_cls._ticket = guarded_ticket
    coordinator_cls.admit = guarded_admit
    setattr(coordinator_cls, _GUARD_MARKER, True)


_install()


__all__ = []
