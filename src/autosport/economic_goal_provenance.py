"""Deterministic provenance identity for the canonical EconomicGoalContract.

This module is evidence-only: it derives an immutable identity from the existing
canonical EconomicGoalContract payload and never becomes a second economic or
persistence authority. The durable contract remains owned by EconomicGoalStore.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Final

from .economic_goal import (
    EconomicGoalContract,
    EconomicGoalContractError,
    _canonical_contract_snapshot,
)
from .economic_goal_store import economic_goal_to_payload


PROVENANCE_SCHEMA: Final = "autosport.economic_goal_provenance"
PROVENANCE_SCHEMA_VERSION: Final = 1
_MAX_PROVENANCE_IDENTITY_CHARS: Final = 512


class EconomicGoalProvenanceError(ValueError):
    """Raised when economic-goal provenance evidence is malformed or mismatched."""


@dataclass(frozen=True, slots=True)
class EconomicGoalProvenance:
    """Immutable evidence identity for one persisted goal-contract revision."""

    schema: str
    schema_version: int
    goal_id: str
    revision: int
    bankroll_id: str
    contract_sha256: str

    def __post_init__(self) -> None:
        _validate_provenance_bound(self)
    @property
    def decision_identity(self) -> str:
        """Return an immutable, revision-specific identity suitable for evidence binding."""

        return _decision_identity_bound(self)


_PROVENANCE_CONTRACT_FIELD_NAMES: Final = (
    "goal_id", "revision", "bankroll_id", "currency", "objective",
    "max_stake_fraction", "max_stake_amount", "max_session_loss_fraction",
    "max_day_loss_fraction", "max_drawdown_fraction", "max_capital_at_risk_fraction",
    "max_event_concentration_fraction", "max_market_concentration_fraction",
    "max_provider_concentration_fraction", "max_sport_concentration_fraction",
    "max_turnover_fraction", "max_risk_of_ruin", "max_execution_slippage_fraction",
    "max_quote_age_seconds", "minimum_data_quality", "max_concurrent_positions",
    "max_parlay_legs", "automation_level", "emergency_stop",
    "blocked_sports", "blocked_providers", "blocked_markets",
)

_PROVENANCE_FIELD_NAMES: Final = (
    "schema", "schema_version", "goal_id", "revision", "bankroll_id",
    "contract_sha256",
)

_CANONICAL_PROVENANCE_FIELD_GETTERS: Final = tuple(
    (name, EconomicGoalProvenance.__dict__[name].__get__)
    for name in _PROVENANCE_FIELD_NAMES
)

def _canonical_provenance_snapshot(
    provenance: EconomicGoalProvenance,
    _field_getters=_CANONICAL_PROVENANCE_FIELD_GETTERS,
) -> tuple[object, ...]:
    return tuple(
        getter(provenance, EconomicGoalProvenance)
        for _, getter in _field_getters
    )

def _validate_provenance_bound(
    self: EconomicGoalProvenance,
    _schema=PROVENANCE_SCHEMA,
    _schema_version=PROVENANCE_SCHEMA_VERSION,
    _max_identity_chars=_MAX_PROVENANCE_IDENTITY_CHARS,
    _error_type=EconomicGoalProvenanceError,
) -> None:
    """Validate provenance through captured slot descriptors."""
    if type(self) is not EconomicGoalProvenance:
        raise _error_type("provenance must use the exact evidence type")
    schema, schema_version, goal_id, revision, bankroll_id, contract_sha256 = _canonical_provenance_snapshot(self)
    if type(schema) is not str or schema != _schema:
        raise _error_type("unsupported provenance schema")
    if type(schema_version) is not int or schema_version != _schema_version:
        raise _error_type("unsupported provenance schema version")
    for name, value in (("goal_id", goal_id), ("bankroll_id", bankroll_id)):
        if type(value) is not str or not value:
            raise _error_type(f"{name} must be a non-empty string")
        if value != value.strip():
            raise _error_type(f"{name} must be canonical text")
        if len(value) > _max_identity_chars:
            raise _error_type(f"{name} exceeds the identity size limit")
        if "\x00" in value:
            raise _error_type(f"{name} must not contain NUL")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise _error_type(f"{name} must be valid UTF-8 text") from exc
    if type(revision) is not int or revision <= 0:
        raise _error_type("revision must be a positive integer")
    if (
        type(contract_sha256) is not str
        or len(contract_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in contract_sha256)
    ):
        raise _error_type("contract_sha256 must be lowercase SHA-256 hex")

def _decision_identity_bound(
    self: EconomicGoalProvenance,
    _validator=_validate_provenance_bound,
) -> str:
    _validator(self)
    before = _canonical_provenance_snapshot(self)
    _validator(self)
    after = _canonical_provenance_snapshot(self)
    if before != after:
        raise EconomicGoalProvenanceError(
            "economic-goal provenance changed during identity derivation"
        )
    _, _, goal_id, revision, _, contract_sha256 = after
    return f"{goal_id}@{revision}:{contract_sha256}"

EconomicGoalProvenance.__post_init__ = _validate_provenance_bound

_CANONICAL_GOAL_TYPE: Final = EconomicGoalContract
_CANONICAL_GOAL_VALIDATOR: Final = EconomicGoalContract.__post_init__
_CANONICAL_PROVENANCE_TYPE: Final = EconomicGoalProvenance
_CANONICAL_PROVENANCE_VALIDATOR: Final = _validate_provenance_bound


def _canonical_json(
    payload: object,
    _dumps=json.dumps,
    _error_type=EconomicGoalProvenanceError,
) -> bytes:
    try:
        return _dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _error_type("economic-goal payload is not canonically serializable") from exc


def _contract_sha256_bound(
    contract: EconomicGoalContract,
    _goal_type=_CANONICAL_GOAL_TYPE,
    _goal_validator=_CANONICAL_GOAL_VALIDATOR,
    _payload_encoder=economic_goal_to_payload,
    _json_encoder=_canonical_json,
    _sha256=hashlib.sha256,
    _goal_error=EconomicGoalContractError,
) -> str:
    """Hash the exact canonical persisted representation of ``contract``."""

    if type(contract) is not _goal_type:
        raise _goal_error("provenance hashing requires an EconomicGoalContract")
    _goal_validator(contract)
    return _sha256(_json_encoder(_payload_encoder(contract))).hexdigest()


def _provenance_for_bound(
    contract: EconomicGoalContract,
    _goal_type=_CANONICAL_GOAL_TYPE,
    _goal_validator=_CANONICAL_GOAL_VALIDATOR,
    _provenance_type=_CANONICAL_PROVENANCE_TYPE,
    _provenance_validator=_CANONICAL_PROVENANCE_VALIDATOR,
    _contract_sha256=_contract_sha256_bound,
    _schema=PROVENANCE_SCHEMA,
    _schema_version=PROVENANCE_SCHEMA_VERSION,
    _goal_error=EconomicGoalContractError,
) -> EconomicGoalProvenance:
    """Derive immutable provenance identity without introducing another authority."""

    if type(contract) is not _goal_type:
        raise _goal_error("provenance requires an EconomicGoalContract")
    _goal_validator(contract)
    before = _canonical_contract_snapshot(contract)
    _goal_validator(contract)
    after = _canonical_contract_snapshot(contract)
    if before != after:
        raise _goal_error("economic goal changed during provenance derivation")
    values = dict(zip(_PROVENANCE_CONTRACT_FIELD_NAMES, after))
    contract_sha256 = _contract_sha256(contract)
    final_snapshot = _canonical_contract_snapshot(contract)
    if after != final_snapshot:
        raise _goal_error("economic goal changed during provenance derivation")
    provenance = _provenance_type(
        schema=_schema,
        schema_version=_schema_version,
        goal_id=values["goal_id"],
        revision=values["revision"],
        bankroll_id=values["bankroll_id"],
        contract_sha256=contract_sha256,
    )
    _provenance_validator(provenance)
    return provenance


def _verify_provenance_bound(
    contract: EconomicGoalContract,
    provenance: EconomicGoalProvenance,
    _goal_type=_CANONICAL_GOAL_TYPE,
    _provenance_type=_CANONICAL_PROVENANCE_TYPE,
    _goal_validator=_CANONICAL_GOAL_VALIDATOR,
    _provenance_validator=_CANONICAL_PROVENANCE_VALIDATOR,
    _contract_sha256=_contract_sha256_bound,
    _goal_error=EconomicGoalContractError,
    _provenance_error=EconomicGoalProvenanceError,
) -> None:
    """Fail closed when provenance no longer matches the canonical contract."""

    if type(contract) is not _goal_type:
        raise _goal_error("provenance verification requires an EconomicGoalContract")
    if type(provenance) is not _provenance_type:
        raise _provenance_error("provenance must be EconomicGoalProvenance")

    _goal_validator(contract)
    contract_before = _canonical_contract_snapshot(contract)
    _provenance_validator(provenance)
    provenance_before = _canonical_provenance_snapshot(provenance)

    _goal_validator(contract)
    contract_after = _canonical_contract_snapshot(contract)
    _provenance_validator(provenance)
    provenance_after = _canonical_provenance_snapshot(provenance)

    if contract_before != contract_after:
        raise _goal_error("economic goal changed during provenance verification")
    if provenance_before != provenance_after:
        raise _provenance_error("economic-goal provenance changed during verification")

    _, _, goal_id, revision, bankroll_id = contract_after[:5]
    proven_goal_id = provenance_after[2]
    proven_revision = provenance_after[3]
    proven_bankroll_id = provenance_after[4]
    proven_contract_sha256 = provenance_after[5]

    if proven_goal_id != goal_id:
        raise _provenance_error("provenance goal_id mismatch")
    if proven_revision != revision:
        raise _provenance_error("provenance revision mismatch")
    if proven_bankroll_id != bankroll_id:
        raise _provenance_error("provenance bankroll_id mismatch")
    actual = _contract_sha256(contract)
    final_contract_snapshot = _canonical_contract_snapshot(contract)
    if contract_after != final_contract_snapshot:
        raise _goal_error("economic goal changed during provenance verification")
    if proven_contract_sha256 != actual:
        raise _provenance_error("provenance contract_sha256 mismatch")


# Public authority-bearing operations intentionally expose no injectable helper
# parameters.  The closures capture the already-bound canonical implementations,
# so rebinding module aliases cannot redirect dispatch and callers cannot supply
# forged validator/hash/type dependencies through hidden keyword arguments.
def _bind_contract_operation(operation):
    def bound(contract: EconomicGoalContract):
        return operation(contract)

    return bound


def _bind_provenance_verifier(operation):
    def bound(
        contract: EconomicGoalContract,
        provenance: EconomicGoalProvenance,
    ) -> None:
        operation(contract, provenance)

    return bound


contract_sha256 = _bind_contract_operation(_contract_sha256_bound)
provenance_for = _bind_contract_operation(_provenance_for_bound)
verify_provenance = _bind_provenance_verifier(_verify_provenance_bound)
