"""Durable, versioned persistence for the owner EconomicGoalContract.

This module is intentionally a narrow authority boundary. It serializes the
existing typed :class:`EconomicGoalContract` without turning that contract into
an executable risk policy. Automatic writers may only publish an immediate
non-expanding successor of the already persisted owner contract.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from .economic_goal import (
    AutomationLevel,
    EconomicGoalContract,
    EconomicGoalContractError,
    EconomicObjective,
    validate_automatic_transition,
)
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .workspace_lock import WorkspaceEconomicLock


ECONOMIC_GOAL_SCHEMA: Final = "autosport.economic_goal_contract"
ECONOMIC_GOAL_SCHEMA_VERSION: Final = 1

_CONTRACT_KEYS: Final = frozenset(
    {
        "goal_id",
        "revision",
        "bankroll_id",
        "currency",
        "objective",
        "max_stake_fraction",
        "max_stake_amount",
        "max_session_loss_fraction",
        "max_day_loss_fraction",
        "max_drawdown_fraction",
        "max_capital_at_risk_fraction",
        "max_event_concentration_fraction",
        "max_market_concentration_fraction",
        "max_provider_concentration_fraction",
        "max_sport_concentration_fraction",
        "max_turnover_fraction",
        "max_risk_of_ruin",
        "max_execution_slippage_fraction",
        "max_quote_age_seconds",
        "minimum_data_quality",
        "max_concurrent_positions",
        "max_parlay_legs",
        "automation_level",
        "emergency_stop",
        "blocked_sports",
        "blocked_providers",
        "blocked_markets",
    }
)
_ROOT_KEYS: Final = frozenset({"schema", "schema_version", "contract"})
_DECIMAL_FIELDS: Final = (
    "max_stake_fraction",
    "max_session_loss_fraction",
    "max_day_loss_fraction",
    "max_drawdown_fraction",
    "max_capital_at_risk_fraction",
    "max_event_concentration_fraction",
    "max_market_concentration_fraction",
    "max_provider_concentration_fraction",
    "max_sport_concentration_fraction",
    "max_turnover_fraction",
    "max_risk_of_ruin",
    "max_execution_slippage_fraction",
    "max_quote_age_seconds",
    "minimum_data_quality",
)
_RESTRICTION_FIELDS: Final = (
    "blocked_sports",
    "blocked_providers",
    "blocked_markets",
)


def _require_exact_keys(
    name: str, value: dict[str, object], expected: frozenset[str]
) -> None:
    keys = frozenset(value)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise EconomicGoalContractError(
            f"{name} keys must match schema exactly; missing={missing!r} extra={extra!r}"
        )


def _decimal_text(name: str, value: object) -> Decimal:
    if not isinstance(value, str):
        raise EconomicGoalContractError(f"{name} must be a Decimal string")
    if not value or value != value.strip():
        raise EconomicGoalContractError(f"{name} must be a canonical Decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise EconomicGoalContractError(f"{name} is not a valid Decimal string") from exc
    if not parsed.is_finite():
        raise EconomicGoalContractError(f"{name} must be finite")
    if str(parsed) != value:
        raise EconomicGoalContractError(f"{name} must use canonical Decimal text")
    return parsed


def _restriction_set(name: str, value: object) -> frozenset[str]:
    if not isinstance(value, list):
        raise EconomicGoalContractError(f"{name} must be a sorted JSON array")
    if any(not isinstance(item, str) for item in value):
        raise EconomicGoalContractError(f"{name} must contain only strings")
    if value != sorted(value) or len(value) != len(set(value)):
        raise EconomicGoalContractError(
            f"{name} must be sorted and contain unique strings"
        )
    return frozenset(value)


def economic_goal_to_payload(contract: EconomicGoalContract) -> dict[str, object]:
    """Return the canonical schema-v1 JSON payload for ``contract``."""

    if not isinstance(contract, EconomicGoalContract):
        raise EconomicGoalContractError(
            "economic goal persistence requires an EconomicGoalContract"
        )

    body: dict[str, object] = {
        "goal_id": contract.goal_id,
        "revision": contract.revision,
        "bankroll_id": contract.bankroll_id,
        "currency": contract.currency,
        "objective": contract.objective.value,
        "max_stake_fraction": str(contract.max_stake_fraction),
        "max_stake_amount": (
            None if contract.max_stake_amount is None else str(contract.max_stake_amount)
        ),
        "max_session_loss_fraction": str(contract.max_session_loss_fraction),
        "max_day_loss_fraction": str(contract.max_day_loss_fraction),
        "max_drawdown_fraction": str(contract.max_drawdown_fraction),
        "max_capital_at_risk_fraction": str(contract.max_capital_at_risk_fraction),
        "max_event_concentration_fraction": str(
            contract.max_event_concentration_fraction
        ),
        "max_market_concentration_fraction": str(
            contract.max_market_concentration_fraction
        ),
        "max_provider_concentration_fraction": str(
            contract.max_provider_concentration_fraction
        ),
        "max_sport_concentration_fraction": str(
            contract.max_sport_concentration_fraction
        ),
        "max_turnover_fraction": str(contract.max_turnover_fraction),
        "max_risk_of_ruin": str(contract.max_risk_of_ruin),
        "max_execution_slippage_fraction": str(
            contract.max_execution_slippage_fraction
        ),
        "max_quote_age_seconds": str(contract.max_quote_age_seconds),
        "minimum_data_quality": str(contract.minimum_data_quality),
        "max_concurrent_positions": contract.max_concurrent_positions,
        "max_parlay_legs": contract.max_parlay_legs,
        "automation_level": int(contract.automation_level),
        "emergency_stop": contract.emergency_stop,
        "blocked_sports": sorted(contract.blocked_sports),
        "blocked_providers": sorted(contract.blocked_providers),
        "blocked_markets": sorted(contract.blocked_markets),
    }
    return {
        "schema": ECONOMIC_GOAL_SCHEMA,
        "schema_version": ECONOMIC_GOAL_SCHEMA_VERSION,
        "contract": body,
    }


def economic_goal_from_payload(payload: object) -> EconomicGoalContract:
    """Decode schema-v1 persistence input and fail closed on any ambiguity."""

    if not isinstance(payload, dict) or not all(
        isinstance(key, str) for key in payload
    ):
        raise EconomicGoalContractError("economic goal payload must be a JSON object")
    root: dict[str, object] = payload
    _require_exact_keys("economic goal payload", root, _ROOT_KEYS)

    if root["schema"] != ECONOMIC_GOAL_SCHEMA:
        raise EconomicGoalContractError("unsupported economic goal schema")
    version = root["schema_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != ECONOMIC_GOAL_SCHEMA_VERSION
    ):
        raise EconomicGoalContractError("unsupported economic goal schema_version")

    raw_contract = root["contract"]
    if not isinstance(raw_contract, dict) or not all(
        isinstance(key, str) for key in raw_contract
    ):
        raise EconomicGoalContractError("contract must be a JSON object")
    body: dict[str, object] = raw_contract
    _require_exact_keys("contract", body, _CONTRACT_KEYS)

    decoded = dict(body)
    for field in _DECIMAL_FIELDS:
        decoded[field] = _decimal_text(field, body[field])
    if body["max_stake_amount"] is None:
        decoded["max_stake_amount"] = None
    else:
        decoded["max_stake_amount"] = _decimal_text(
            "max_stake_amount", body["max_stake_amount"]
        )

    try:
        decoded["objective"] = EconomicObjective(body["objective"])
    except (TypeError, ValueError) as exc:
        raise EconomicGoalContractError("unsupported economic objective") from exc

    automation = body["automation_level"]
    if isinstance(automation, bool) or not isinstance(automation, int):
        raise EconomicGoalContractError("automation_level must be an integer")
    try:
        decoded["automation_level"] = AutomationLevel(automation)
    except ValueError as exc:
        raise EconomicGoalContractError("unsupported automation_level") from exc

    for field in _RESTRICTION_FIELDS:
        decoded[field] = _restriction_set(field, body[field])

    try:
        return EconomicGoalContract(**decoded)  # type: ignore[arg-type]
    except EconomicGoalContractError:
        raise
    except (TypeError, ValueError) as exc:
        raise EconomicGoalContractError("malformed economic goal contract") from exc


def economic_goal_from_json(text: str) -> EconomicGoalContract:
    """Decode one strict JSON document into a validated contract."""

    if not isinstance(text, str):
        raise EconomicGoalContractError("economic goal JSON must be text")
    try:
        payload = strict_json_loads(text)
    except (TypeError, ValueError) as exc:
        raise EconomicGoalContractError("invalid economic goal JSON") from exc
    return economic_goal_from_payload(payload)


class EconomicGoalStore:
    """Workspace-local durable owner-contract store.

    ``initialize_owner`` is creation-only. Automatic actors have only
    ``persist_automatic_successor`` which, under the canonical workspace economic
    writer lock, reloads the durable predecessor and applies the monotonic authority
    validator before atomic publication. The lock makes validation and publication
    one cooperating-writer critical section so stale concurrent revisions cannot
    overwrite a newly tightened authority state.
    """

    FILE_NAME: Final = "economic_goal_contract.json"

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.path = self.workspace / self.FILE_NAME

    def load(self) -> EconomicGoalContract:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise EconomicGoalContractError(
                f"cannot read persisted economic goal: {exc}"
            ) from exc
        return economic_goal_from_json(text)

    def initialize_owner(self, contract: EconomicGoalContract) -> None:
        """Create the first owner contract while holding the economic writer lock."""

        with WorkspaceEconomicLock(self.workspace):
            if self.path.exists():
                raise EconomicGoalContractError(
                    "persisted economic goal already exists; owner replacement requires "
                    "a separate authority boundary"
                )
            atomic_write_json(self.path, economic_goal_to_payload(contract))

    def persist_automatic_successor(self, candidate: EconomicGoalContract) -> None:
        """Publish one machine revision only when durable authority cannot expand."""

        with WorkspaceEconomicLock(self.workspace):
            previous = self.load()
            validate_automatic_transition(previous, candidate)
            atomic_write_json(self.path, economic_goal_to_payload(candidate))
