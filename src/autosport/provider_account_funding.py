"""Causal provider-account funding feasibility for supervised execution plans.

This module closes only the read-only capital-location question from #1587.  It
consumes already-canonical supervised-plan, execution-scope, and reconciled
account evidence.  It deliberately does not create a bankroll, transfer,
reservation, provider-write, or execution authority.

A positive local-balance result means only that the exact required accounts
show enough provider-native available balance at the supplied causal cut.  It
is *not* sufficient to execute: an atomic reservation/headroom authority and
an explicit freshness policy remain separate required gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json

from .bookmaker_account_reconciliation import ReconciledAccountState
from .bookmaker_capability import (
    BookmakerBalanceObservation,
    BookmakerCapability,
)
from .execution_scope_admission import ExecutionScopePreAdmission
from .supervised_execution import BoundSupervisedExecutionPlan


_MAX_FUNDING_DECIMAL_TEXT_LENGTH = 8192\n\n\nclass ProviderAccountFundingError(ValueError):
    """Canonical funding inputs conflict or cannot be interpreted safely."""


class AccountFundingStatus(str, Enum):
    FUNDED_AT_REQUIRED_ACCOUNT = "FUNDED_AT_REQUIRED_ACCOUNT"
    UNDERFUNDED_AT_REQUIRED_ACCOUNT = "UNDERFUNDED_AT_REQUIRED_ACCOUNT"
    ACCOUNT_STATE_MISSING = "ACCOUNT_STATE_MISSING"
    ACCOUNT_STATE_PARTIAL = "ACCOUNT_STATE_PARTIAL"
    BALANCE_EVIDENCE_MISSING = "BALANCE_EVIDENCE_MISSING"
    BALANCE_EVIDENCE_FUTURE = "BALANCE_EVIDENCE_FUTURE"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    CURRENCY_CONFLICT = "CURRENCY_CONFLICT"
    SIDE_UNSUPPORTED = "SIDE_UNSUPPORTED"


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ProviderAccountFundingError(
            f"{field} must be non-empty canonical text"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProviderAccountFundingError(
            f"{field} must be UTF-8 encodable"
        ) from exc
    return value


def _instant(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderAccountFundingError(
            f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderAccountFundingError(
            f"{field} must be timezone-aware"
        )
    return parsed


def _money(value: object, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise ProviderAccountFundingError(
            f"{field} must be an exact finite non-negative Decimal"
        )
    sign, digits, exponent = value.as_tuple()
    if sign or not isinstance(exponent, int):
        raise ProviderAccountFundingError(
            f"{field} must be an exact finite non-negative Decimal"
        )
    if exponent >= 0:
        rendered_length = len(digits) + exponent
    else:
        point = len(digits) + exponent
        rendered_length = len(digits) + 1 if point > 0 else 2 - exponent
    if rendered_length > _MAX_FUNDING_DECIMAL_TEXT_LENGTH:
        raise ProviderAccountFundingError(
            f"{field} fixed-point representation exceeds resource limit"
        )
    return value


def _canonical_decimal(value: Decimal) -> str:
    _money(value, "money")
    sign, digits, exponent = value.as_tuple()
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    if coefficient == 0:
        return "0"
    while coefficient % 10 == 0:
        coefficient //= 10
        exponent += 1
    body = str(coefficient)
    if exponent >= 0:
        body = body + ("0" * exponent)
    else:
        point = len(body) + exponent
        body = (
            f"{body[:point]}.{body[point:]}"
            if point > 0
            else f"0.{('0' * -point)}{body}"
        )
    return f"-{body}" if sign else body


def _exact_sum(values: tuple[Decimal, ...]) -> Decimal:
    """Sum exact finite non-negative Decimals without ambient Context rounding."""

    if not values:
        return Decimal(0)
    parts: list[tuple[int, int]] = []
    common_exponent: int | None = None
    for value in values:
        _money(value, "required capital")
        sign, digits, exponent = value.as_tuple()
        if sign or not isinstance(exponent, int):
            raise ProviderAccountFundingError(
                "required capital must be finite and non-negative"
            )
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        parts.append((coefficient, exponent))
        common_exponent = (
            exponent
            if common_exponent is None
            else min(common_exponent, exponent)
        )
    assert common_exponent is not None
    total = sum(
        coefficient * (10 ** (exponent - common_exponent))
        for coefficient, exponent in parts
    )
    if total == 0:
        return Decimal(0)
    while total % 10 == 0:
        total //= 10
        common_exponent += 1
    return Decimal((0, tuple(int(char) for char in str(total)), common_exponent))


def _canonical_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProviderAccountFundingError(
            "funding evidence is not canonical JSON"
        ) from exc
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderAccountFundingRow:
    venue_id: str
    account_id: str
    action_ids: tuple[str, ...]
    requested_currency: str | None
    account_snapshot_id: str | None
    balance_observation_id: str | None
    available_balance: Decimal | None
    required_capital: Decimal | None
    status: AccountFundingStatus

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        if (
            type(self.action_ids) is not tuple
            or not self.action_ids
            or any(type(item) is not str or not item for item in self.action_ids)
            or tuple(sorted(self.action_ids)) != self.action_ids
            or len(set(self.action_ids)) != len(self.action_ids)
        ):
            raise ProviderAccountFundingError(
                "action_ids must be a unique sorted non-empty tuple"
            )
        if self.requested_currency is not None:
            _text(self.requested_currency, "requested_currency")
        if self.account_snapshot_id is not None:
            _text(self.account_snapshot_id, "account_snapshot_id")
        if self.balance_observation_id is not None:
            _text(self.balance_observation_id, "balance_observation_id")
        if self.available_balance is not None:
            _money(self.available_balance, "available_balance")
        if self.required_capital is not None:
            _money(self.required_capital, "required_capital")
        if type(self.status) is not AccountFundingStatus:
            raise ProviderAccountFundingError(
                "status must be AccountFundingStatus"
            )


@dataclass(frozen=True, slots=True)
class ProviderAccountFundingAssessment:
    supervised_plan_id: str
    execution_plan_fingerprint: str
    economic_goal_contract_sha256: str
    scope_assessment_sha256s: tuple[str, ...]
    rows: tuple[ProviderAccountFundingRow, ...]
    local_balance_sufficient: bool
    causal_balance_evidence_complete: bool
    reservation_boundary_proven: bool = False
    freshness_policy_proven: bool = False
    grants_transfer_authority: bool = False
    grants_execution_authority: bool = False

    def __post_init__(self) -> None:
        for field in (
            "supervised_plan_id",
            "execution_plan_fingerprint",
            "economic_goal_contract_sha256",
        ):
            _text(getattr(self, field), field)
        if (
            type(self.scope_assessment_sha256s) is not tuple
            or not self.scope_assessment_sha256s
            or any(type(item) is not str or not item for item in self.scope_assessment_sha256s)
        ):
            raise ProviderAccountFundingError(
                "scope_assessment_sha256s must be a non-empty tuple"
            )
        if type(self.rows) is not tuple or not self.rows:
            raise ProviderAccountFundingError("rows must be a non-empty tuple")
        if any(type(item) is not ProviderAccountFundingRow for item in self.rows):
            raise ProviderAccountFundingError(
                "rows must contain exact ProviderAccountFundingRow values"
            )
        for field in (
            "local_balance_sufficient",
            "causal_balance_evidence_complete",
            "reservation_boundary_proven",
            "freshness_policy_proven",
            "grants_transfer_authority",
            "grants_execution_authority",
        ):
            if type(getattr(self, field)) is not bool:
                raise ProviderAccountFundingError(f"{field} must be bool")
        if (
            self.reservation_boundary_proven
            or self.freshness_policy_proven
            or self.grants_transfer_authority
            or self.grants_execution_authority
        ):
            raise ProviderAccountFundingError(
                "schema v1 cannot claim reservation/freshness/transfer/execution authority"
            )

    @property
    def execution_funding_proven(self) -> bool:
        """Schema v1 cannot authorize execution from read-only balance evidence."""

        return False

    @property
    def assessment_sha256(self) -> str:
        return _canonical_digest(
            {
                "schema": "autosport.provider_account_funding_assessment",
                "schema_version": 1,
                "supervised_plan_id": self.supervised_plan_id,
                "execution_plan_fingerprint": self.execution_plan_fingerprint,
                "economic_goal_contract_sha256": self.economic_goal_contract_sha256,
                "scope_assessment_sha256s": list(self.scope_assessment_sha256s),
                "rows": [
                    {
                        "venue_id": row.venue_id,
                        "account_id": row.account_id,
                        "action_ids": list(row.action_ids),
                        "requested_currency": row.requested_currency,
                        "account_snapshot_id": row.account_snapshot_id,
                        "balance_observation_id": row.balance_observation_id,
                        "available_balance": (
                            None
                            if row.available_balance is None
                            else _canonical_decimal(row.available_balance)
                        ),
                        "required_capital": (
                            None
                            if row.required_capital is None
                            else _canonical_decimal(row.required_capital)
                        ),
                        "status": row.status.value,
                    }
                    for row in self.rows
                ],
                "local_balance_sufficient": self.local_balance_sufficient,
                "causal_balance_evidence_complete": self.causal_balance_evidence_complete,
                "reservation_boundary_proven": self.reservation_boundary_proven,
                "freshness_policy_proven": self.freshness_policy_proven,
                "execution_funding_proven": self.execution_funding_proven,
                "grants_transfer_authority": self.grants_transfer_authority,
                "grants_execution_authority": self.grants_execution_authority,
            }
        )


def assess_provider_account_funding(
    *,
    bound_plan: BoundSupervisedExecutionPlan,
    scope_assessments: tuple[ExecutionScopePreAdmission, ...],
    account_states: tuple[ReconciledAccountState, ...],
) -> ProviderAccountFundingAssessment:
    """Project exact per-account balance sufficiency without minting authority.

    The projection deliberately has two permanent schema-v1 negative gates:
    reservation_boundary_proven=False and freshness_policy_proven=False.  A
    consumer must therefore never treat local_balance_sufficient as permission
    to place a wager.  Those gates are left for their existing canonical
    authorities rather than reimplemented here.
    """

    if type(bound_plan) is not BoundSupervisedExecutionPlan:
        raise ProviderAccountFundingError(
            "bound_plan must be exact BoundSupervisedExecutionPlan"
        )
    if type(scope_assessments) is not tuple or any(
        type(item) is not ExecutionScopePreAdmission
        for item in scope_assessments
    ):
        raise ProviderAccountFundingError(
            "scope_assessments must contain exact ExecutionScopePreAdmission values"
        )
    if type(account_states) is not tuple or any(
        type(item) is not ReconciledAccountState for item in account_states
    ):
        raise ProviderAccountFundingError(
            "account_states must contain exact ReconciledAccountState values"
        )

    bound_plan.verify_binding()
    plan = bound_plan.execution_plan
    actions = {action.action_id: action for action in plan.actions}
    assessments_by_action: dict[str, ExecutionScopePreAdmission] = {}
    for assessment in scope_assessments:
        action_id = assessment.scope.action_id
        if action_id in assessments_by_action:
            raise ProviderAccountFundingError(
                "scope assessment action identity is duplicated"
            )
        if action_id not in actions:
            raise ProviderAccountFundingError(
                "scope assessment references action outside supervised plan"
            )
        if assessment.supervised_plan_id != plan.plan_id:
            raise ProviderAccountFundingError(
                "scope assessment supervised plan identity mismatch"
            )
        if not assessment.supervised_action_binding_structurally_valid:
            raise ProviderAccountFundingError(
                "scope assessment lacks structural supervised-action binding"
            )
        action = actions[action_id]
        if (
            assessment.scope.event_id,
            assessment.scope.market_id,
            assessment.scope.selection_id,
            assessment.scope.side,
        ) != (
            action.event_id,
            action.market_id,
            action.selection_id,
            action.side,
        ):
            raise ProviderAccountFundingError(
                "scope assessment action semantics mismatch"
            )
        assessments_by_action[action_id] = assessment

    if set(assessments_by_action) != set(actions):
        raise ProviderAccountFundingError(
            "scope assessments must exactly cover supervised plan actions"
        )

    states_by_account: dict[tuple[str, str], ReconciledAccountState] = {}
    for state in account_states:
        key = (state.venue_id, state.account_id)
        if key in states_by_account:
            raise ProviderAccountFundingError(
                "reconciled account state identity is duplicated"
            )
        states_by_account[key] = state

    grouped_actions: dict[tuple[str, str], list[str]] = {}
    for action in plan.actions:
        grouped_actions.setdefault(
            (action.bookmaker_id, action.account_id), []
        ).append(action.action_id)

    rows: list[ProviderAccountFundingRow] = []
    for key in sorted(grouped_actions):
        venue_id, account_id = key
        action_ids = tuple(sorted(grouped_actions[key]))
        account_actions = tuple(actions[action_id] for action_id in action_ids)
        account_scopes = tuple(
            assessments_by_action[action_id] for action_id in action_ids
        )
        currencies = {item.scope.currency for item in account_scopes}
        requested_currency = next(iter(currencies)) if len(currencies) == 1 else None

        if any(action.side != "BACK" for action in account_actions):
            rows.append(
                ProviderAccountFundingRow(
                    venue_id=venue_id,
                    account_id=account_id,
                    action_ids=action_ids,
                    requested_currency=requested_currency,
                    account_snapshot_id=None,
                    balance_observation_id=None,
                    available_balance=None,
                    required_capital=None,
                    status=AccountFundingStatus.SIDE_UNSUPPORTED,
                )
            )
            continue

        required = _exact_sum(
            tuple(action.requested_stake for action in account_actions)
        )
        state = states_by_account.get(key)
        if state is None:
            rows.append(
                ProviderAccountFundingRow(
                    venue_id=venue_id,
                    account_id=account_id,
                    action_ids=action_ids,
                    requested_currency=requested_currency,
                    account_snapshot_id=None,
                    balance_observation_id=None,
                    available_balance=None,
                    required_capital=required,
                    status=AccountFundingStatus.ACCOUNT_STATE_MISSING,
                )
            )
            continue

        profile = bound_plan.profile_for(venue_id, account_id)
        if (
            state.adapter_id != profile.adapter_id
            or state.profile_id != profile.profile_sha256
        ):
            raise ProviderAccountFundingError(
                "reconciled account state does not match supervised profile binding"
            )

        if len(currencies) != 1:
            rows.append(
                ProviderAccountFundingRow(
                    venue_id=venue_id,
                    account_id=account_id,
                    action_ids=action_ids,
                    requested_currency=None,
                    account_snapshot_id=state.snapshot_id,
                    balance_observation_id=(
                        None
                        if state.latest_balance_observation is None
                        else state.latest_balance_observation.observation_id
                    ),
                    available_balance=(
                        None
                        if state.latest_balance_observation is None
                        else state.latest_balance_observation.available_balance
                    ),
                    required_capital=required,
                    status=AccountFundingStatus.CURRENCY_CONFLICT,
                )
            )
            continue

        if BookmakerCapability.BALANCE_READ not in state.observed_capabilities:
            balance = state.latest_balance_observation
            rows.append(
                ProviderAccountFundingRow(
                    venue_id=venue_id,
                    account_id=account_id,
                    action_ids=action_ids,
                    requested_currency=requested_currency,
                    account_snapshot_id=state.snapshot_id,
                    balance_observation_id=(
                        None if balance is None else balance.observation_id
                    ),
                    available_balance=(
                        None if balance is None else balance.available_balance
                    ),
                    required_capital=required,
                    status=AccountFundingStatus.ACCOUNT_STATE_PARTIAL,
                )
            )
            continue

        balance = state.latest_balance_observation
        if type(balance) is not BookmakerBalanceObservation:
            rows.append(
                ProviderAccountFundingRow(
                    venue_id=venue_id,
                    account_id=account_id,
                    action_ids=action_ids,
                    requested_currency=requested_currency,
                    account_snapshot_id=state.snapshot_id,
                    balance_observation_id=None,
                    available_balance=None,
                    required_capital=required,
                    status=AccountFundingStatus.BALANCE_EVIDENCE_MISSING,
                )
            )
            continue
        if (
            balance.venue_id,
            balance.account_id,
            balance.adapter_id,
        ) != (
            venue_id,
            account_id,
            state.adapter_id,
        ):
            raise ProviderAccountFundingError(
                "balance observation identity conflicts with reconciled account state"
            )

        if balance.currency != requested_currency:
            rows.append(
                ProviderAccountFundingRow(
                    venue_id=venue_id,
                    account_id=account_id,
                    action_ids=action_ids,
                    requested_currency=requested_currency,
                    account_snapshot_id=state.snapshot_id,
                    balance_observation_id=balance.observation_id,
                    available_balance=balance.available_balance,
                    required_capital=required,
                    status=AccountFundingStatus.CURRENCY_MISMATCH,
                )
            )
            continue

        cutoffs = tuple(
            _instant(item.scope.evidence_cutoff, "evidence_cutoff")
            for item in account_scopes
        )
        if (
            any(_instant(state.observed_at, "account observed_at") > cutoff for cutoff in cutoffs)
            or any(
                _instant(balance.observed_at, "balance observed_at") > cutoff
                for cutoff in cutoffs
            )
        ):
            rows.append(
                ProviderAccountFundingRow(
                    venue_id=venue_id,
                    account_id=account_id,
                    action_ids=action_ids,
                    requested_currency=requested_currency,
                    account_snapshot_id=state.snapshot_id,
                    balance_observation_id=balance.observation_id,
                    available_balance=balance.available_balance,
                    required_capital=required,
                    status=AccountFundingStatus.BALANCE_EVIDENCE_FUTURE,
                )
            )
            continue

        status = (
            AccountFundingStatus.FUNDED_AT_REQUIRED_ACCOUNT
            if balance.available_balance >= required
            else AccountFundingStatus.UNDERFUNDED_AT_REQUIRED_ACCOUNT
        )
        rows.append(
            ProviderAccountFundingRow(
                venue_id=venue_id,
                account_id=account_id,
                action_ids=action_ids,
                requested_currency=requested_currency,
                account_snapshot_id=state.snapshot_id,
                balance_observation_id=balance.observation_id,
                available_balance=balance.available_balance,
                required_capital=required,
                status=status,
            )
        )

    rows_tuple = tuple(rows)
    local_balance_sufficient = all(
        row.status is AccountFundingStatus.FUNDED_AT_REQUIRED_ACCOUNT
        for row in rows_tuple
    )
    causal_balance_evidence_complete = all(
        row.status
        in {
            AccountFundingStatus.FUNDED_AT_REQUIRED_ACCOUNT,
            AccountFundingStatus.UNDERFUNDED_AT_REQUIRED_ACCOUNT,
        }
        for row in rows_tuple
    )
    return ProviderAccountFundingAssessment(
        supervised_plan_id=plan.plan_id,
        execution_plan_fingerprint=plan.fingerprint,
        economic_goal_contract_sha256=bound_plan.economic_goal_contract_sha256,
        scope_assessment_sha256s=tuple(
            assessments_by_action[action_id].assessment_sha256
            for action_id in sorted(assessments_by_action)
        ),
        rows=rows_tuple,
        local_balance_sufficient=local_balance_sufficient,
        causal_balance_evidence_complete=causal_balance_evidence_complete,
    )
