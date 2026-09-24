from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Underflow,
    localcontext,
)
from enum import Enum


_LIQUIDITY_DECIMAL_CONTEXT = Context(
    prec=50,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[InvalidOperation, DivisionByZero, Overflow, Underflow, Inexact, Rounded],
)


def _require_money(value: object, label: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{label} must be a finite Decimal")
    if value < 0:
        raise ValueError(f"{label} must be non-negative")
    return value


def _require_canonical_text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty canonical string")
    return value


def _require_provider_id(value: object) -> str:
    return _require_canonical_text(value, "provider_id")


def _require_account_id(value: object) -> str:
    return _require_canonical_text(value, "account_id")


def _require_bankroll_id(value: object) -> str:
    return _require_canonical_text(value, "bankroll_id")


def _require_currency(value: object) -> str:
    return _require_canonical_text(value, "currency")


def _canonical_decimal(value: Decimal) -> str:
    sign, digits_tuple, exponent = value.as_tuple()
    digits = list(digits_tuple)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if not digits:
        return "0"
    coefficient = "".join(str(digit) for digit in digits)
    prefix = "-" if sign else ""
    return f"{prefix}{coefficient}e{exponent}"


def _arithmetic_error(exc: DecimalException) -> ValueError:
    return ValueError(
        "liquidity economics are not exactly representable in the canonical Decimal context"
    )


class LiquidityTransferDirection(str, Enum):
    BANK_TO_PROVIDER = "bank_to_provider"
    PROVIDER_TO_BANK = "provider_to_bank"


class LiquidityTransferStage(str, Enum):
    SWEEP_EXCESS = "sweep_excess"
    RESTORE_MINIMUM = "restore_minimum"
    RESTORE_TARGET = "restore_target"


@dataclass(frozen=True, slots=True)
class ProviderLiquidityAccount:
    provider_id: str
    account_id: str
    currency: str
    cash_balance: Decimal
    committed_cash: Decimal
    minimum_cash: Decimal
    target_cash: Decimal
    funding_priority: int = 0

    def __post_init__(self) -> None:
        _require_provider_id(self.provider_id)
        _require_account_id(self.account_id)
        _require_currency(self.currency)
        cash_balance = _require_money(self.cash_balance, "cash_balance")
        committed_cash = _require_money(self.committed_cash, "committed_cash")
        minimum_cash = _require_money(self.minimum_cash, "minimum_cash")
        target_cash = _require_money(self.target_cash, "target_cash")
        if committed_cash > cash_balance:
            raise ValueError("committed_cash cannot exceed cash_balance")
        if minimum_cash > target_cash:
            raise ValueError("minimum_cash cannot exceed target_cash")
        if isinstance(self.funding_priority, bool) or not isinstance(
            self.funding_priority, int
        ):
            raise ValueError("funding_priority must be an integer")


@dataclass(frozen=True, slots=True)
class GlobalLiquiditySnapshot:
    bankroll_id: str
    currency: str
    bank_cash: Decimal
    bank_reserve: Decimal
    provider_accounts: tuple[ProviderLiquidityAccount, ...]

    def __post_init__(self) -> None:
        _require_bankroll_id(self.bankroll_id)
        currency = _require_currency(self.currency)
        _require_money(self.bank_cash, "bank_cash")
        _require_money(self.bank_reserve, "bank_reserve")
        if type(self.provider_accounts) is not tuple:
            raise ValueError("provider_accounts must be a tuple")
        if not all(
            isinstance(account, ProviderLiquidityAccount)
            for account in self.provider_accounts
        ):
            raise ValueError(
                "provider_accounts must contain ProviderLiquidityAccount values"
            )
        mismatched = [
            (account.provider_id, account.account_id, account.currency)
            for account in self.provider_accounts
            if account.currency != currency
        ]
        if mismatched:
            raise ValueError(
                "all provider accounts must use the exact snapshot currency; "
                f"mismatched accounts: {mismatched!r}"
            )
        account_keys = [
            (account.provider_id, account.account_id)
            for account in self.provider_accounts
        ]
        if len(account_keys) != len(set(account_keys)):
            raise ValueError("provider account identities must be unique")


@dataclass(frozen=True, slots=True)
class LiquidityTransfer:
    provider_id: str
    account_id: str
    currency: str
    direction: LiquidityTransferDirection
    stage: LiquidityTransferStage
    amount: Decimal

    def __post_init__(self) -> None:
        _require_provider_id(self.provider_id)
        _require_account_id(self.account_id)
        _require_currency(self.currency)
        if not isinstance(self.direction, LiquidityTransferDirection):
            raise ValueError("direction must be a LiquidityTransferDirection")
        if not isinstance(self.stage, LiquidityTransferStage):
            raise ValueError("stage must be a LiquidityTransferStage")
        _require_money(self.amount, "transfer amount")
        if self.amount == 0:
            raise ValueError("transfer amount must be greater than zero")


@dataclass(frozen=True, slots=True)
class ProviderLiquidityBalance:
    provider_id: str
    account_id: str
    currency: str
    cash_balance: Decimal

    def __post_init__(self) -> None:
        _require_provider_id(self.provider_id)
        _require_account_id(self.account_id)
        _require_currency(self.currency)
        _require_money(self.cash_balance, "cash_balance")


@dataclass(frozen=True, slots=True)
class ProviderLiquidityShortfall:
    provider_id: str
    account_id: str
    currency: str
    amount: Decimal

    def __post_init__(self) -> None:
        _require_provider_id(self.provider_id)
        _require_account_id(self.account_id)
        _require_currency(self.currency)
        _require_money(self.amount, "shortfall amount")
        if self.amount == 0:
            raise ValueError("shortfall amount must be greater than zero")


@dataclass(frozen=True, slots=True)
class LiquidityRebalancePlan:
    evidence_id: str
    bankroll_id: str
    currency: str
    transfers: tuple[LiquidityTransfer, ...]
    balances_after: tuple[ProviderLiquidityBalance, ...]
    minimum_shortfalls_after: tuple[ProviderLiquidityShortfall, ...]
    target_shortfalls_after: tuple[ProviderLiquidityShortfall, ...]
    bank_cash_before: Decimal
    bank_cash_after: Decimal
    bank_reserve: Decimal
    total_cash_before: Decimal
    total_cash_after: Decimal

    @property
    def conserves_cash(self) -> bool:
        return self.total_cash_before == self.total_cash_after

    @property
    def minimums_fully_funded(self) -> bool:
        return not self.minimum_shortfalls_after

    @property
    def targets_fully_funded(self) -> bool:
        return not self.target_shortfalls_after

    @property
    def bank_reserve_satisfied(self) -> bool:
        return self.bank_cash_after >= self.bank_reserve

    @property
    def balances_verified(self) -> bool:
        """Caller-supplied balance assertions are never provider-source verification."""
        return False

    @property
    def commitments_verified(self) -> bool:
        """Caller-supplied commitments are never canonical exposure verification."""
        return False

    @property
    def funds_reserved(self) -> bool:
        """A plan never reserves or moves cash."""
        return False

    @property
    def grants_transfer_authority(self) -> bool:
        """A plan never authorizes provider transfers."""
        return False

    @property
    def grants_execution_authority(self) -> bool:
        """A plan is evidence/recommendation only; it never authorizes a wager."""
        return False


def _plan_evidence_id(
    snapshot: GlobalLiquiditySnapshot,
    transfers: tuple[LiquidityTransfer, ...],
    balances_after: tuple[ProviderLiquidityBalance, ...],
) -> str:
    payload = {
        "bankroll_id": snapshot.bankroll_id,
        "currency": snapshot.currency,
        "bank_cash": _canonical_decimal(snapshot.bank_cash),
        "bank_reserve": _canonical_decimal(snapshot.bank_reserve),
        "providers": [
            {
                "provider_id": account.provider_id,
                "account_id": account.account_id,
                "currency": account.currency,
                "cash_balance": _canonical_decimal(account.cash_balance),
                "committed_cash": _canonical_decimal(account.committed_cash),
                "minimum_cash": _canonical_decimal(account.minimum_cash),
                "target_cash": _canonical_decimal(account.target_cash),
                "funding_priority": account.funding_priority,
            }
            for account in sorted(
                snapshot.provider_accounts,
                key=lambda account: (account.provider_id, account.account_id),
            )
        ],
        "transfers": [
            {
                "provider_id": transfer.provider_id,
                "account_id": transfer.account_id,
                "currency": transfer.currency,
                "direction": transfer.direction.value,
                "stage": transfer.stage.value,
                "amount": _canonical_decimal(transfer.amount),
            }
            for transfer in transfers
        ],
        "balances_after": [
            {
                "provider_id": balance.provider_id,
                "account_id": balance.account_id,
                "currency": balance.currency,
                "cash_balance": _canonical_decimal(balance.cash_balance),
            }
            for balance in balances_after
        ],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def build_liquidity_rebalance_plan(
    snapshot: GlobalLiquiditySnapshot,
) -> LiquidityRebalancePlan:
    """Build a deterministic, assertion-only provider-liquidity rebalance plan.

    The model is deliberately single-currency. Every provider account must carry the
    exact same denomination as the global-bank snapshot; cross-currency arithmetic is
    rejected rather than implicitly scalarized.

    Policy:
    1. Sweep only provider cash above both target and currently committed cash.
    2. Preserve the global bank reserve when deploying cash.
    3. Restore provider minimums before targets.
    4. Under scarcity, fund higher ``funding_priority`` first, then provider/account id.

    Inputs are caller assertions. The returned plan does not verify provider balances,
    commitments or reserves and grants no authority to transfer cash or place a wager.
    Any execution-capable consumer must independently re-resolve canonical account and
    exposure evidence before acting.
    """

    if not isinstance(snapshot, GlobalLiquiditySnapshot):
        raise ValueError("snapshot must be a GlobalLiquiditySnapshot")

    try:
        with localcontext(_LIQUIDITY_DECIMAL_CONTEXT):
            providers_by_id = sorted(
                snapshot.provider_accounts,
                key=lambda account: (account.provider_id, account.account_id),
            )
            funding_order = sorted(
                snapshot.provider_accounts,
                key=lambda account: (
                    -account.funding_priority,
                    account.provider_id,
                    account.account_id,
                ),
            )
            balances = {
                (account.provider_id, account.account_id): account.cash_balance
                for account in providers_by_id
            }
            transfers: list[LiquidityTransfer] = []
            bank_cash = snapshot.bank_cash
            total_cash_before = bank_cash + sum(
                (account.cash_balance for account in providers_by_id), Decimal("0")
            )

            for account in providers_by_id:
                key = (account.provider_id, account.account_id)
                withdrawal_floor = max(account.target_cash, account.committed_cash)
                excess = balances[key] - withdrawal_floor
                if excess > 0:
                    balances[key] -= excess
                    bank_cash += excess
                    transfers.append(
                        LiquidityTransfer(
                            provider_id=account.provider_id,
                            account_id=account.account_id,
                            currency=snapshot.currency,
                            direction=LiquidityTransferDirection.PROVIDER_TO_BANK,
                            stage=LiquidityTransferStage.SWEEP_EXCESS,
                            amount=excess,
                        )
                    )

            deployable = max(Decimal("0"), bank_cash - snapshot.bank_reserve)

            for stage, target_attribute in (
                (LiquidityTransferStage.RESTORE_MINIMUM, "minimum_cash"),
                (LiquidityTransferStage.RESTORE_TARGET, "target_cash"),
            ):
                for account in funding_order:
                    key = (account.provider_id, account.account_id)
                    target = getattr(account, target_attribute)
                    deficit = target - balances[key]
                    if deficit <= 0 or deployable <= 0:
                        continue
                    amount = min(deficit, deployable)
                    balances[key] += amount
                    bank_cash -= amount
                    deployable -= amount
                    transfers.append(
                        LiquidityTransfer(
                            provider_id=account.provider_id,
                            account_id=account.account_id,
                            currency=snapshot.currency,
                            direction=LiquidityTransferDirection.BANK_TO_PROVIDER,
                            stage=stage,
                            amount=amount,
                        )
                    )

            balances_after = tuple(
                ProviderLiquidityBalance(
                    account.provider_id,
                    account.account_id,
                    snapshot.currency,
                    balances[(account.provider_id, account.account_id)],
                )
                for account in providers_by_id
            )
            minimum_shortfalls = tuple(
                ProviderLiquidityShortfall(
                    account.provider_id,
                    account.account_id,
                    snapshot.currency,
                    account.minimum_cash
                    - balances[(account.provider_id, account.account_id)],
                )
                for account in providers_by_id
                if balances[(account.provider_id, account.account_id)]
                < account.minimum_cash
            )
            target_shortfalls = tuple(
                ProviderLiquidityShortfall(
                    account.provider_id,
                    account.account_id,
                    snapshot.currency,
                    account.target_cash
                    - balances[(account.provider_id, account.account_id)],
                )
                for account in providers_by_id
                if balances[(account.provider_id, account.account_id)]
                < account.target_cash
            )
            total_cash_after = bank_cash + sum(
                (balance.cash_balance for balance in balances_after), Decimal("0")
            )
            if total_cash_after != total_cash_before:
                raise ValueError("liquidity rebalance plan does not conserve total cash")

            transfer_tuple = tuple(transfers)
            evidence_id = _plan_evidence_id(snapshot, transfer_tuple, balances_after)
            return LiquidityRebalancePlan(
                evidence_id=evidence_id,
                bankroll_id=snapshot.bankroll_id,
                currency=snapshot.currency,
                transfers=transfer_tuple,
                balances_after=balances_after,
                minimum_shortfalls_after=minimum_shortfalls,
                target_shortfalls_after=target_shortfalls,
                bank_cash_before=snapshot.bank_cash,
                bank_cash_after=bank_cash,
                bank_reserve=snapshot.bank_reserve,
                total_cash_before=total_cash_before,
                total_cash_after=total_cash_after,
            )
    except DecimalException as exc:
        raise _arithmetic_error(exc) from exc
