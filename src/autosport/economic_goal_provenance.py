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
        error_type = EconomicGoalProvenanceError

        def canonical_identity_text(name: str, value: object) -> None:
            if type(value) is not str or not value:
                raise error_type(f"{name} must be a non-empty string")
            if value != value.strip():
                raise error_type(f"{name} must be canonical text")
            if len(value) > 512:
                raise error_type(f"{name} exceeds the identity size limit")
            if "\x00" in value:
                raise error_type(f"{name} must not contain NUL")
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise error_type(f"{name} must be valid UTF-8 text") from exc

        if type(self.schema) is not str or self.schema != "autosport.economic_goal_provenance":
            raise error_type("unsupported provenance schema")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise error_type("unsupported provenance schema version")
        canonical_identity_text("goal_id", self.goal_id)
        if type(self.revision) is not int or self.revision <= 0:
            raise error_type("revision must be a positive integer")
        canonical_identity_text("bankroll_id", self.bankroll_id)
        if (
            type(self.contract_sha256) is not str
            or len(self.contract_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.contract_sha256)
        ):
            raise error_type("contract_sha256 must be lowercase SHA-256 hex")

    @property
    def decision_identity(self) -> str:
        """Return an immutable, revision-specific identity suitable for evidence binding."""

        __class__.__post_init__(self)
        return f"{self.goal_id}@{self.revision}:{self.contract_sha256}"


_CANONICAL_GOAL_TYPE: Final = EconomicGoalContract
_CANONICAL_GOAL_VALIDATOR: Final = EconomicGoalContract.__post_init__
_CANONICAL_PROVENANCE_TYPE: Final = EconomicGoalProvenance
_CANONICAL_PROVENANCE_VALIDATOR: Final = EconomicGoalProvenance.__post_init__
_CANONICAL_GOAL_SERIALIZER: Final = economic_goal_to_payload
_CANONICAL_JSON_DUMPS: Final = json.dumps
_CANONICAL_SHA256: Final = hashlib.sha256


def _canonical_json(payload: object) -> bytes:
    try:
        return _CANONICAL_JSON_DUMPS(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise EconomicGoalProvenanceError("economic-goal payload is not canonically serializable") from exc


_CANONICAL_JSON_ENCODER: Final = _canonical_json


def contract_sha256(contract: EconomicGoalContract) -> str:
    """Hash the exact canonical persisted representation of ``contract``."""

    if type(contract) is not _CANONICAL_GOAL_TYPE:
        raise EconomicGoalContractError(
            "provenance hashing requires a canonical EconomicGoalContract"
        )
    _CANONICAL_GOAL_VALIDATOR(contract)
    return _CANONICAL_SHA256(_CANONICAL_JSON_ENCODER(_CANONICAL_GOAL_SERIALIZER(contract))).hexdigest()


_CANONICAL_CONTRACT_HASHER: Final = contract_sha256


def provenance_for(contract: EconomicGoalContract) -> EconomicGoalProvenance:
    """Derive immutable provenance identity without introducing another authority."""

    if type(contract) is not _CANONICAL_GOAL_TYPE:
        raise EconomicGoalContractError(
            "provenance requires a canonical EconomicGoalContract"
        )
    _CANONICAL_GOAL_VALIDATOR(contract)
    return _CANONICAL_PROVENANCE_TYPE(
        schema="autosport.economic_goal_provenance",
        schema_version=1,
        goal_id=contract.goal_id,
        revision=contract.revision,
        bankroll_id=contract.bankroll_id,
        contract_sha256=_CANONICAL_CONTRACT_HASHER(contract),
    )


def verify_provenance(
    contract: EconomicGoalContract,
    provenance: EconomicGoalProvenance,
) -> None:
    """Fail closed when provenance no longer matches the canonical contract."""

    if type(contract) is not _CANONICAL_GOAL_TYPE:
        raise EconomicGoalProvenanceError(
            "contract must be canonical EconomicGoalContract"
        )
    _CANONICAL_GOAL_VALIDATOR(contract)
    if type(provenance) is not _CANONICAL_PROVENANCE_TYPE:
        raise EconomicGoalProvenanceError(
            "provenance must be canonical EconomicGoalProvenance"
        )
    _CANONICAL_PROVENANCE_VALIDATOR(provenance)
    if provenance.goal_id != contract.goal_id:
        raise EconomicGoalProvenanceError("provenance goal_id mismatch")
    if provenance.revision != contract.revision:
        raise EconomicGoalProvenanceError("provenance revision mismatch")
    if provenance.bankroll_id != contract.bankroll_id:
        raise EconomicGoalProvenanceError("provenance bankroll_id mismatch")
    actual = _CANONICAL_CONTRACT_HASHER(contract)
    if provenance.contract_sha256 != actual:
        raise EconomicGoalProvenanceError("provenance contract_sha256 mismatch")
