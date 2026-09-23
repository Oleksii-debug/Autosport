"""Fail closed on impossible Betfair LAY matched prices before evidence issuance.

The canonical provider verifier already rejects a BACK match below the submitted
limit. Betfair's normal Exchange best-price semantics are side-aware: a LAY customer
seeks the lower price, so a standard LAY match above the submitted limit is likewise
worse and cannot be accepted as authoritative execution economics.

This guard is deliberately reject-only and runs before the canonical verifier. It
must never mint evidence itself: otherwise a rejected-but-issued object could be
recovered from a traceback and reused as provider authority.
"""

from __future__ import annotations

from . import supervised_execution as _supervised
from . import supervised_provider_evidence as _provider


def _install_betfair_lay_price_bound_guard() -> None:
    provider_verify = _provider.verify_betfair_provider_state
    if getattr(provider_verify, "_betfair_lay_price_bound_guard_installed", False):
        return

    if _supervised.verify_betfair_provider_state is not provider_verify:
        raise _provider.ProviderEvidenceError(
            "supervised execution provider-verifier alias drifted before LAY guard"
        )

    def reject_worse_lay_price(
        action: _provider.ExecutionAction,
        readback: _provider.BetfairExecutionReadbackEnvelope,
    ) -> None:
        # Do not dispatch through caller-polymorphic objects before the canonical
        # verifier performs its own exact-type/origin checks. This prefilter only
        # inspects exact canonical DTOs/built-in tuples and can only reject, never
        # strengthen truth.
        if (
            type(action) is not _provider.ExecutionAction
            or type(readback) is not _provider.BetfairExecutionReadbackEnvelope
            or action.side != "LAY"
        ):
            return

        current_pages = readback.current_pages
        cleared_entries = readback.cleared_pages_by_status
        if type(current_pages) is not tuple or type(cleared_entries) is not tuple:
            return

        for page in current_pages:
            if type(page) is not _provider.BetfairCurrentOrderPage:
                return
            orders = page.orders
            if type(orders) is not tuple:
                return
            for order in orders:
                if type(order) is not _provider.BetfairCurrentOrderObservation:
                    return
                if (
                    order.side == "LAY"
                    and order.size_matched > 0
                    and order.average_price_matched > action.requested_odds
                ):
                    raise _provider.ProviderEvidenceError(
                        "provider matched price is worse than submitted Betfair LAY limit"
                    )

        for entry in cleared_entries:
            if type(entry) is not tuple or len(entry) != 2:
                return
            status, pages = entry
            if status != "SETTLED":
                continue
            if type(pages) is not tuple:
                return
            for page in pages:
                if type(page) is not _provider.BetfairClearedOrderPage:
                    return
                orders = page.orders
                if type(orders) is not tuple:
                    return
                for order in orders:
                    if type(order) is not _provider.BetfairClearedOrderObservation:
                        return
                    if (
                        order.side == "LAY"
                        and order.size_settled > 0
                        and order.price_matched > action.requested_odds
                    ):
                        raise _provider.ProviderEvidenceError(
                            "provider matched price is worse than submitted Betfair LAY limit"
                        )

    def guarded_verify(
        action: _provider.ExecutionAction,
        profile: _provider.BookmakerCapabilityProfile,
        *,
        expected_profile_sha256: str,
        readback: _provider.BetfairExecutionReadbackEnvelope,
        expected_provider_order_ref: str | None = None,
    ) -> _provider.VerifiedProviderState:
        reject_worse_lay_price(action, readback)
        return provider_verify(
            action,
            profile,
            expected_profile_sha256=expected_profile_sha256,
            readback=readback,
            expected_provider_order_ref=expected_provider_order_ref,
        )

    guarded_verify._betfair_lay_price_bound_guard_installed = True
    _provider.verify_betfair_provider_state = guarded_verify
    _supervised.verify_betfair_provider_state = guarded_verify


_install_betfair_lay_price_bound_guard()
del _install_betfair_lay_price_bound_guard
