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
_MAX_ECONOMIC_GOAL_DECIMAL_TEXT_CHARS: Final = 512
_MAX_ECONOMIC_GOAL_RESTRICTION_MEMBERS: Final = 1024
_MAX_ECONOMIC_GOAL_RESTRICTION_TEXT_CHARS: Final = 512

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
    name: str,
    value: dict[str, object],
    expected: frozenset[str],
    _error_type=EconomicGoalContractError,
) -> None:
    keys = frozenset(value)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise _error_type(
            f"{name} keys must match schema exactly; missing={missing!r} extra={extra!r}"
        )


def _decimal_text(
    name: str,
    value: object,
    _decimal_type=Decimal,
    _invalid_operation=InvalidOperation,
    _error_type=EconomicGoalContractError,
) -> Decimal:
    if type(value) is not str:
        raise _error_type(f"{name} must be a Decimal string")
    if not value or value != value.strip():
        raise _error_type(f"{name} must be a canonical Decimal string")
    if len(value) > _MAX_ECONOMIC_GOAL_DECIMAL_TEXT_CHARS:
        raise _error_type(
            f"{name} Decimal text exceeds the canonical size limit"
        )
    try:
        parsed = _decimal_type(value)
    except _invalid_operation as exc:
        raise _error_type(f"{name} is not a valid Decimal string") from exc
    if not parsed.is_finite():
        raise _error_type(f"{name} must be finite")
    if str(parsed) != value:
        raise _error_type(f"{name} must use canonical Decimal text")
    return parsed


def _restriction_set(
    name: str,
    value: object,
    _error_type=EconomicGoalContractError,
) -> frozenset[str]:
    if type(value) is not list:
        raise _error_type(f"{name} must be a sorted JSON array")
    if len(value) > _MAX_ECONOMIC_GOAL_RESTRICTION_MEMBERS:
        raise _error_type(
            f"{name} exceeds the canonical restriction-count limit"
        )
    for item in value:
        if type(item) is not str:
            raise _error_type(f"{name} must contain only strings")
        if (
            not item
            or item != item.strip()
            or len(item) > _MAX_ECONOMIC_GOAL_RESTRICTION_TEXT_CHARS
            or "\x00" in item
        ):
            raise _error_type(
                f"{name} contains non-canonical restriction text"
            )
    if value != sorted(value) or len(value) != len(set(value)):
        raise _error_type(
            f"{name} must be sorted and contain unique strings"
        )
    return frozenset(value)


_CANONICAL_GOAL_TYPE: Final = EconomicGoalContract
_CANONICAL_GOAL_VALIDATOR: Final = EconomicGoalContract.__post_init__
_CANONICAL_OBJECTIVE_TYPE: Final = EconomicObjective
_CANONICAL_AUTOMATION_TYPE: Final = AutomationLevel
_CANONICAL_TRANSITION_VALIDATOR: Final = validate_automatic_transition
_CANONICAL_STRICT_JSON_LOADS: Final = strict_json_loads
_CANONICAL_ATOMIC_WRITE_JSON: Final = atomic_write_json
_CANONICAL_WORKSPACE_LOCK_TYPE: Final = WorkspaceEconomicLock
_CANONICAL_PATH_TYPE: Final = Path


def economic_goal_to_payload(
    contract: EconomicGoalContract,
    _goal_type=EconomicGoalContract,
    _goal_validator=EconomicGoalContract.__post_init__,
    _error_type=EconomicGoalContractError,
) -> dict[str, object]:
    """Return the canonical schema-v1 JSON payload for ``contract``."""

    if type(contract) is not _goal_type:
        raise _error_type(
            "economic goal persistence requires an EconomicGoalContract"
        )
    _goal_validator(contract)

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
        "schema": _schema,
        "schema_version": _schema_version,
        "contract": body,
    }


def economic_goal_from_payload(
    payload: object,
    _goal_type=EconomicGoalContract,
    _goal_error=EconomicGoalContractError,
    _objective_type=EconomicObjective,
    _automation_type=AutomationLevel,
    _exact_keys=_require_exact_keys,
    _decimal_decoder=_decimal_text,
    _restriction_decoder=_restriction_set,
    _root_keys=_ROOT_KEYS,
    _contract_keys=_CONTRACT_KEYS,
    _decimal_fields=_DECIMAL_FIELDS,
    _restriction_fields=_RESTRICTION_FIELDS,
) -> EconomicGoalContract:
    """Decode schema-v1 persistence input and fail closed on any ambiguity."""

    if type(payload) is not dict or not all(
        type(key) is str for key in payload
    ):
        raise _goal_error("economic goal payload must be a JSON object")
    root: dict[str, object] = payload
    _exact_keys("economic goal payload", root, _root_keys)

    if type(root["schema"]) is not str or root["schema"] != _schema:
        raise _goal_error("unsupported economic goal schema")
    version = root["schema_version"]
    if type(version) is not int or version != _schema_version:
        raise _goal_error("unsupported economic goal schema_version")

    raw_contract = root["contract"]
    if type(raw_contract) is not dict or not all(
        type(key) is str for key in raw_contract
    ):
        raise _goal_error("contract must be a JSON object")
    body: dict[str, object] = raw_contract
    _exact_keys("contract", body, _contract_keys)

    decoded = dict(body)
    for field in _decimal_fields:
        decoded[field] = _decimal_decoder(field, body[field])
    if body["max_stake_amount"] is None:
        decoded["max_stake_amount"] = None
    else:
        decoded["max_stake_amount"] = _decimal_decoder(
            "max_stake_amount", body["max_stake_amount"]
        )

    if type(body["objective"]) is not str:
        raise _goal_error("objective must be a string")
    try:
        decoded["objective"] = _objective_type(body["objective"])
    except (TypeError, ValueError) as exc:
        raise _goal_error("unsupported economic objective") from exc

    automation = body["automation_level"]
    if type(automation) is not int:
        raise _goal_error("automation_level must be an integer")
    try:
        decoded["automation_level"] = _automation_type(automation)
    except ValueError as exc:
        raise _goal_error("unsupported automation_level") from exc

    for field in _restriction_fields:
        decoded[field] = _restriction_decoder(field, body[field])

    try:
        return _goal_type(**decoded)  # type: ignore[arg-type]
    except _goal_error:
        raise
    except (TypeError, ValueError) as exc:
        raise _goal_error("malformed economic goal contract") from exc


def economic_goal_from_json(
    text: str,
    _loads=strict_json_loads,
    _payload_decoder=economic_goal_from_payload,
    _error_type=EconomicGoalContractError,
) -> EconomicGoalContract:
    """Decode one strict JSON document into a validated contract."""

    if type(text) is not str:
        raise _error_type("economic goal JSON must be text")
    try:
        payload = _loads(text)
    except (TypeError, ValueError) as exc:
        raise _error_type("invalid economic goal JSON") from exc
    return _payload_decoder(payload)


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

    def __init__(
        self,
        workspace: str | Path,
        _path_type=_CANONICAL_PATH_TYPE,
    ) -> None:
        self.workspace = _path_type(workspace)
        self.path = self.workspace / self.FILE_NAME

    def load(
        self,
        _json_decoder=economic_goal_from_json,
    ) -> EconomicGoalContract:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise EconomicGoalContractError(
                f"cannot read persisted economic goal: {exc}"
            ) from exc
        return _json_decoder(text)

    def initialize_owner(
        self,
        contract: EconomicGoalContract,
        _lock_type=_CANONICAL_WORKSPACE_LOCK_TYPE,
        _payload_encoder=economic_goal_to_payload,
        _writer=_CANONICAL_ATOMIC_WRITE_JSON,
    ) -> None:
        """Create the first owner contract while holding the economic writer lock."""

        with _lock_type(self.workspace):
            if self.path.exists():
                raise EconomicGoalContractError(
                    "persisted economic goal already exists; owner replacement requires "
                    "a separate authority boundary"
                )
            _writer(self.path, _payload_encoder(contract))

    def persist_automatic_successor(
        self,
        candidate: EconomicGoalContract,
        _lock_type=_CANONICAL_WORKSPACE_LOCK_TYPE,
        _transition_validator=_CANONICAL_TRANSITION_VALIDATOR,
        _payload_encoder=economic_goal_to_payload,
        _writer=_CANONICAL_ATOMIC_WRITE_JSON,
    ) -> None:
        """Publish one machine revision only when durable authority cannot expand."""

        with _lock_type(self.workspace):
            previous = self.load()
            _transition_validator(previous, candidate)
            _writer(self.path, _payload_encoder(candidate))
