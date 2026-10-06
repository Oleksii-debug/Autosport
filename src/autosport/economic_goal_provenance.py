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

from .economic_goal import EconomicGoalContract, EconomicGoalContractError
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
        if type(self.schema) is not str or self.schema != PROVENANCE_SCHEMA:
            raise EconomicGoalProvenanceError("unsupported provenance schema")
        if type(self.schema_version) is not int or self.schema_version != PROVENANCE_SCHEMA_VERSION:
            raise EconomicGoalProvenanceError("unsupported provenance schema version")
        for name, value in (("goal_id", self.goal_id), ("bankroll_id", self.bankroll_id)):
            if type(value) is not str or not value:
                raise EconomicGoalProvenanceError(f"{name} must be a non-empty string")
            if value != value.strip():
                raise EconomicGoalProvenanceError(f"{name} must be canonical text")
            if len(value) > _MAX_PROVENANCE_IDENTITY_CHARS:
                raise EconomicGoalProvenanceError(f"{name} exceeds the identity size limit")
            if "\x00" in value:
                raise EconomicGoalProvenanceError(f"{name} must not contain NUL")
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise EconomicGoalProvenanceError(
                    f"{name} must be valid UTF-8 text"
                ) from exc
        if type(self.revision) is not int or self.revision <= 0:
            raise EconomicGoalProvenanceError("revision must be a positive integer")
        if (
            type(self.contract_sha256) is not str
            or len(self.contract_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.contract_sha256)
        ):
            raise EconomicGoalProvenanceError("contract_sha256 must be lowercase SHA-256 hex")

    @property
    def decision_identity(self, _validator=__post_init__) -> str:
        """Return an immutable, revision-specific identity suitable for evidence binding."""

        _validator(self)
        return f"{self.goal_id}@{self.revision}:{self.contract_sha256}"


_CANONICAL_GOAL_TYPE: Final = EconomicGoalContract
_CANONICAL_GOAL_VALIDATOR: Final = EconomicGoalContract.__post_init__
_CANONICAL_PROVENANCE_TYPE: Final = EconomicGoalProvenance
_CANONICAL_PROVENANCE_VALIDATOR: Final = EconomicGoalProvenance.__post_init__


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


def contract_sha256(
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


def provenance_for(
    contract: EconomicGoalContract,
    _goal_type=_CANONICAL_GOAL_TYPE,
    _goal_validator=_CANONICAL_GOAL_VALIDATOR,
    _provenance_type=_CANONICAL_PROVENANCE_TYPE,
    _contract_sha256=contract_sha256,
    _schema=PROVENANCE_SCHEMA,
    _schema_version=PROVENANCE_SCHEMA_VERSION,
    _goal_error=EconomicGoalContractError,
) -> EconomicGoalProvenance:
    """Derive immutable provenance identity without introducing another authority."""

    if type(contract) is not _goal_type:
        raise _goal_error("provenance requires an EconomicGoalContract")
    _goal_validator(contract)
    return _provenance_type(
        schema=_schema,
        schema_version=_schema_version,
        goal_id=contract.goal_id,
        revision=contract.revision,
        bankroll_id=contract.bankroll_id,
        contract_sha256=_contract_sha256(contract),
    )


def verify_provenance(
    contract: EconomicGoalContract,
    provenance: EconomicGoalProvenance,
    _goal_type=_CANONICAL_GOAL_TYPE,
    _provenance_type=_CANONICAL_PROVENANCE_TYPE,
    _goal_validator=_CANONICAL_GOAL_VALIDATOR,
    _provenance_validator=_CANONICAL_PROVENANCE_VALIDATOR,
    _contract_sha256=contract_sha256,
    _goal_error=EconomicGoalContractError,
    _provenance_error=EconomicGoalProvenanceError,
) -> None:
    """Fail closed when provenance no longer matches the canonical contract."""

    if type(contract) is not _goal_type:
        raise _goal_error("provenance verification requires an EconomicGoalContract")
    if type(provenance) is not _provenance_type:
        raise _provenance_error("provenance must be EconomicGoalProvenance")
    _goal_validator(contract)
    _provenance_validator(provenance)
    if provenance.goal_id != contract.goal_id:
        raise _provenance_error("provenance goal_id mismatch")
    if provenance.revision != contract.revision:
        raise _provenance_error("provenance revision mismatch")
    if provenance.bankroll_id != contract.bankroll_id:
        raise _provenance_error("provenance bankroll_id mismatch")
    actual = _contract_sha256(contract)
    if provenance.contract_sha256 != actual:
        raise _provenance_error("provenance contract_sha256 mismatch")
