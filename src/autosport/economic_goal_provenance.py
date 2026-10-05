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

_SHA256 = hashlib.sha256
_JSON_DUMPS = json.dumps
_GOAL_TO_PAYLOAD = economic_goal_to_payload
_ECONOMIC_GOAL_TYPE = EconomicGoalContract
_ECONOMIC_GOAL_ERROR = EconomicGoalContractError


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

    def __post_init__(
        self,
        _schema=PROVENANCE_SCHEMA,
        _version=PROVENANCE_SCHEMA_VERSION,
    ) -> None:
        if self.schema != _schema:
            raise EconomicGoalProvenanceError("unsupported provenance schema")
        if self.schema_version != _version:
            raise EconomicGoalProvenanceError("unsupported provenance schema version")
        if not isinstance(self.goal_id, str) or not self.goal_id:
            raise EconomicGoalProvenanceError("goal_id must be a non-empty string")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision <= 0:
            raise EconomicGoalProvenanceError("revision must be a positive integer")
        if not isinstance(self.bankroll_id, str) or not self.bankroll_id:
            raise EconomicGoalProvenanceError("bankroll_id must be a non-empty string")
        if (
            not isinstance(self.contract_sha256, str)
            or len(self.contract_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.contract_sha256)
        ):
            raise EconomicGoalProvenanceError("contract_sha256 must be lowercase SHA-256 hex")

    @property
    def decision_identity(self) -> str:
        """Return an immutable, revision-specific identity suitable for evidence binding."""

        return f"{self.goal_id}@{self.revision}:{self.contract_sha256}"


def _canonical_json(payload: object, *, _dumps=_JSON_DUMPS) -> bytes:
    try:
        return _dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise EconomicGoalProvenanceError("economic-goal payload is not canonically serializable") from exc


def contract_sha256(
    contract: EconomicGoalContract,
    *,
    _goal_type=_ECONOMIC_GOAL_TYPE,
    _goal_error=_ECONOMIC_GOAL_ERROR,
    _to_payload=_GOAL_TO_PAYLOAD,
    _canonical=_canonical_json,
    _sha256=_SHA256,
) -> str:
    """Hash the exact canonical persisted representation of ``contract``."""

    if not isinstance(contract, _goal_type):
        raise _goal_error("provenance hashing requires an EconomicGoalContract")
    return _sha256(_canonical(_to_payload(contract))).hexdigest()
def provenance_for(
    contract: EconomicGoalContract,
    *,
    _goal_type=_ECONOMIC_GOAL_TYPE,
    _goal_error=_ECONOMIC_GOAL_ERROR,
    _provenance_type=EconomicGoalProvenance,
    _contract_sha=contract_sha256,
) -> EconomicGoalProvenance:
    """Derive immutable provenance identity without introducing another authority."""

    if not isinstance(contract, _goal_type):
        raise _goal_error("provenance requires an EconomicGoalContract")
    return _provenance_type(
        schema=PROVENANCE_SCHEMA,
        schema_version=PROVENANCE_SCHEMA_VERSION,
        goal_id=contract.goal_id,
        revision=contract.revision,
        bankroll_id=contract.bankroll_id,
        contract_sha256=_contract_sha(contract),
    )
def verify_provenance(
    contract: EconomicGoalContract,
    provenance: EconomicGoalProvenance,
    *,
    _goal_type=_ECONOMIC_GOAL_TYPE,
    _goal_error=_ECONOMIC_GOAL_ERROR,
    _provenance_type=EconomicGoalProvenance,
    _contract_sha=contract_sha256,
) -> None:
    """Fail closed when provenance no longer matches the canonical contract."""

    if not isinstance(contract, _goal_type):
        raise _goal_error("contract must be an EconomicGoalContract")
    if not isinstance(provenance, _provenance_type):
        raise EconomicGoalProvenanceError("provenance must be EconomicGoalProvenance")
    if provenance.goal_id != contract.goal_id:
        raise EconomicGoalProvenanceError("provenance goal_id mismatch")
    if provenance.revision != contract.revision:
        raise EconomicGoalProvenanceError("provenance revision mismatch")
    if provenance.bankroll_id != contract.bankroll_id:
        raise EconomicGoalProvenanceError("provenance bankroll_id mismatch")
    actual = _contract_sha(contract)
    if provenance.contract_sha256 != actual:
        raise EconomicGoalProvenanceError("provenance contract_sha256 mismatch")
