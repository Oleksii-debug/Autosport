"""Fail closed when aggregate ticket provenance cannot identify a settlement leg.

PaperTicket intentionally stores provider provenance at ticket scope while TicketLeg
has no provider-source field. A multi-provider ticket is therefore valid durable
portfolio evidence, but one source-scoped outcome cannot prove which leg that source
owns. Reject that ambiguous projection after outcome resolution and before the
resolution can reach optional settlement learning or economic mutation.
"""

from __future__ import annotations

import builtins as _builtins
from typing import Any

from . import continuous_session as _session


def _install() -> None:
    coordinator = _session.ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator, "__dict__")
    original_resolutions = coordinator_dict["_settlement_resolutions"]
    canonical_load_book = coordinator_dict["_load_book"]
    exact_type = type

    # This guard runs after callback-capable outcome resolution. Its fail-closed
    # decision must not late-resolve mutable builtins: a hostile self-restoring len()
    # could otherwise report one provider for a multi-provider ticket and suppress the
    # ambiguity rejection without changing this wrapper's code or closure identity.
    builtins_module = _builtins
    builtins_dict = exact_type(builtins_module).__getattribute__(builtins_module, "__dict__")
    exact_len = builtins_dict["len"]
    exact_any = builtins_dict["any"]

    def require_builtin_authority() -> None:
        if (
            builtins_dict.get("len") is not exact_len
            or builtins_dict.get("any") is not exact_any
        ):
            raise _session.ContinuousSessionError(
                "canonical settlement multi-source builtin authority changed"
            )

    def settlement_resolutions(self: Any, *args: Any, **kwargs: Any):
        # Freeze the direct builtin callables in invocation locals before entering the
        # callback-capable outcome authority. The final Wave-M dispatch descriptor
        # witnesses these long-lived closure cells at method acquisition, but an outcome
        # callback could otherwise pair-rebind both a cell and the matching builtin,
        # execute the forged callable, then self-restore before the descriptor's
        # post-call witness. Locals are not reachable through that long-lived closure.
        expected_len = exact_len
        expected_any = exact_any
        require_builtin_authority()
        resolutions = original_resolutions(self, *args, **kwargs)
        if exact_len is not expected_len or exact_any is not expected_any:
            raise _session.ContinuousSessionError(
                "canonical settlement multi-source builtin authority changed"
            )
        # Outcome resolution is callback-capable. Recheck before the first ambiguity
        # decision and use only the invocation-local exact callables in the trusted pass.
        require_builtin_authority()
        if not resolutions:
            return resolutions

        book = canonical_load_book(self)
        if exact_len is not expected_len or exact_any is not expected_any:
            raise _session.ContinuousSessionError(
                "canonical settlement multi-source builtin authority changed"
            )
        require_builtin_authority()
        for resolution in resolutions:
            for ticket in book.tickets.values():
                if ticket.status.value != "open":
                    continue
                source_ids = ticket.provider_source_ids
                if exact_type(source_ids) is not tuple:
                    raise _session.ContinuousSessionError(
                        "ticket provider source scope must be a canonical tuple"
                    )
                if expected_len(source_ids) <= 1:
                    continue
                for leg in ticket.legs:
                    if leg.quote_key not in resolution.quote_outcomes:
                        continue
                    if expected_any(
                        resolution.event_identity == f"{source_id}:{leg.event_id}"
                        for source_id in source_ids
                    ):
                        raise _session.ContinuousSessionError(
                            "settlement provider source scope is ambiguous for a multi-source paper ticket"
                        )
        if exact_len is not expected_len or exact_any is not expected_any:
            raise _session.ContinuousSessionError(
                "canonical settlement multi-source builtin authority changed"
            )
        require_builtin_authority()
        return resolutions

    settlement_resolutions.__name__ = original_resolutions.__name__
    settlement_resolutions.__qualname__ = original_resolutions.__qualname__
    settlement_resolutions.__doc__ = original_resolutions.__doc__
    settlement_resolutions.__annotations__ = original_resolutions.__annotations__
    type.__setattr__(coordinator, "_settlement_resolutions", settlement_resolutions)


_install()
del _install
