"""Deterministic provider-liquidity review without transfer or execution authority.

This module answers a narrow portfolio question: given provider-local working-cash
targets plus central unallocated cash, where is capital short and where is there
modeled surplus? It deliberately does not read provider accounts, route orders,
move funds, authorize execution, or assert that provider surplus is transferable.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Iterable


ZERO = Decimal("0")


class LiquidityReviewState(str, Enum):
    """Closed states for provider-liquidity review."""

    BALANCED = "BALANCED"
    CENTRAL_CASH_COVERS = "CENTRAL_CASH_COVERS"
    PROVIDER_REBALANCE_CANDIDATE = "PROVIDER_REBALANCE_CANDIDATE"
    GLOBAL_SHORTFALL = "GLOBAL_SHORTFALL"


@dataclass(frozen=True, slots=True)
class ProviderLiquidityAccount:
    """Caller-supplied point-in-time account observation for portfolio review only."""

    provider_id: str
    currency: str
    available_cash: Decimal
    target_working_cash: Decimal


@dataclass(frozen=True, slots=True)
class ProviderLiquidityGap:
    """Deterministic per-provider need/surplus decomposition."""

    provider_id: str
    currency: str
    available_cash: Decimal
    target_working_cash: Decimal
    need: Decimal
    modeled_surplus: Decimal


@dataclass(frozen=True, slots=True)
class ProviderLiquidityReview:
    """Read-only review result.

    ``modeled_provider_surplus`` is not evidence that money can be withdrawn or
    transferred. ``PROVIDER_REBALANCE_CANDIDATE`` therefore remains a review
    state and never authorizes a transfer or an execution.
    """

    state: LiquidityReviewState
    currency: str
    accounts: tuple[ProviderLiquidityGap, ...]
    central_cash: Decimal
    protected_reserve: Decimal
    central_deployable_cash: Decimal
    total_provider_need: Decimal
    modeled_provider_surplus: Decimal
    need_after_central_cash: Decimal
    modeled_shortfall_after_surplus: Decimal
    requires_transfer_feasibility_check: bool
    authorizes_transfer: bool = False
    authorizes_execution: bool = False


def _currency(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("currency must be str")
    currency = value.strip().upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise ValueError("currency must be a 3-letter ASCII code")
    return currency


def _money(value: Decimal, *, field: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    if value < ZERO:
        raise ValueError(f"{field} must be non-negative")
    return value


def review_provider_liquidity(
    *,
    currency: str,
    central_cash: Decimal,
    protected_reserve: Decimal,
    accounts: Iterable[ProviderLiquidityAccount],
) -> ProviderLiquidityReview:
    """Review deployability versus provider-local working-cash targets.

    All money must be in one explicitly bound currency; mixed-currency arithmetic
    fails closed. Provider-local surplus is only a modeled candidate for
    rebalancing. This function cannot prove transfer capability, transfer latency,
    fees, account limits, provider health, or execution feasibility.
    """

    currency = _currency(currency)
    central_cash = _money(central_cash, field="central_cash")
    protected_reserve = _money(protected_reserve, field="protected_reserve")
    if protected_reserve > central_cash:
        raise ValueError("protected_reserve cannot exceed central_cash")

    gaps: list[ProviderLiquidityGap] = []
    seen: set[str] = set()

    for account in accounts:
        if not isinstance(account, ProviderLiquidityAccount):
            raise TypeError("accounts must contain ProviderLiquidityAccount values")
        provider_id = account.provider_id.strip()
        if not provider_id:
            raise ValueError("provider_id must be non-empty")
        if provider_id in seen:
            raise ValueError(f"duplicate provider_id: {provider_id}")
        seen.add(provider_id)

        account_currency = _currency(account.currency)
        if account_currency != currency:
            raise ValueError(
                f"currency mismatch for {provider_id}: "
                f"expected {currency}, got {account_currency}"
            )
        available = _money(
            account.available_cash,
            field=f"available_cash[{provider_id}]",
        )
        target = _money(
            account.target_working_cash,
            field=f"target_working_cash[{provider_id}]",
        )
        need = max(target - available, ZERO)
        surplus = max(available - target, ZERO)
        gaps.append(
            ProviderLiquidityGap(
                provider_id=provider_id,
                currency=currency,
                available_cash=available,
                target_working_cash=target,
                need=need,
                modeled_surplus=surplus,
            )
        )

    gaps.sort(key=lambda item: item.provider_id)
    total_need = sum((gap.need for gap in gaps), ZERO)
    modeled_surplus = sum((gap.modeled_surplus for gap in gaps), ZERO)
    central_deployable = central_cash - protected_reserve
    need_after_central = max(total_need - central_deployable, ZERO)
    modeled_shortfall = max(need_after_central - modeled_surplus, ZERO)

    if total_need == ZERO:
        state = LiquidityReviewState.BALANCED
    elif need_after_central == ZERO:
        state = LiquidityReviewState.CENTRAL_CASH_COVERS
    elif modeled_shortfall == ZERO:
        state = LiquidityReviewState.PROVIDER_REBALANCE_CANDIDATE
    else:
        state = LiquidityReviewState.GLOBAL_SHORTFALL

    return ProviderLiquidityReview(
        state=state,
        currency=currency,
        accounts=tuple(gaps),
        central_cash=central_cash,
        protected_reserve=protected_reserve,
        central_deployable_cash=central_deployable,
        total_provider_need=total_need,
        modeled_provider_surplus=modeled_surplus,
        need_after_central_cash=need_after_central,
        modeled_shortfall_after_surplus=modeled_shortfall,
        requires_transfer_feasibility_check=(
            need_after_central > ZERO and modeled_surplus > ZERO
        ),
    )
