"""Bind Betfair current-order status to provider-native remaining-size semantics.

Betfair OrderProjection.ALL returns EXECUTABLE and EXECUTION_COMPLETE orders.
Provider documentation defines EXECUTABLE as retaining an unmatched portion and
EXECUTION_COMPLETE as having no remaining unmatched portion.  Reject contradictory
CurrentOrderSummary rows at the canonical DTO boundary before they can participate
in execution readback authority.
"""

from __future__ import annotations

from . import betfair_account_readonly as _betfair


def _install_betfair_current_order_state_guard() -> None:
    observation_type = _betfair.BetfairCurrentOrderObservation
    if vars(observation_type).get("_status_remaining_guard_installed") is True:
        return

    raw_post_init = observation_type.__post_init__

    def guarded_post_init(self: _betfair.BetfairCurrentOrderObservation) -> None:
        raw_post_init(self)
        if self.status == "EXECUTABLE" and self.size_remaining <= 0:
            raise _betfair.BetfairReadOnlyError(
                "EXECUTABLE current order must retain positive size_remaining"
            )
        if self.status == "EXECUTION_COMPLETE" and self.size_remaining != 0:
            raise _betfair.BetfairReadOnlyError(
                "EXECUTION_COMPLETE current order must have zero size_remaining"
            )

    observation_type.__post_init__ = guarded_post_init
    observation_type._status_remaining_guard_installed = True


_install_betfair_current_order_state_guard()
del _install_betfair_current_order_state_guard
