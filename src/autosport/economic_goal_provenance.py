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
from .economic_goal_store import economic_goal_from_payload, economic_goal_to_payload


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

    def __post_init__(
        self,
        _schema=PROVENANCE_SCHEMA,
        _schema_version=PROVENANCE_SCHEMA_VERSION,
        _max_identity_chars=_MAX_PROVENANCE_IDENTITY_CHARS,
        _error_type=EconomicGoalProvenanceError,
    ) -> None:
        if type(self.schema) is not str or self.schema != _schema:
            raise _error_type("unsupported provenance schema")
        if type(self.schema_version) is not int or self.schema_version != _schema_version:
            raise _error_type("unsupported provenance schema version")
        for name, value in (("goal_id", self.goal_id), ("bankroll_id", self.bankroll_id)):
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
                raise _error_type(
                    f"{name} must be valid UTF-8 text"
                ) from exc
        if type(self.revision) is not int or self.revision <= 0:
            raise _error_type("revision must be a positive integer")
        if (
            type(self.contract_sha256) is not str
            or len(self.contract_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.contract_sha256)
        ):
            raise _error_type("contract_sha256 must be lowercase SHA-256 hex")

    @property
    def decision_identity(self, _validator=__post_init__) -> str:
        """Return an immutable, revision-specific identity suitable for evidence binding."""

        _validator(self)
        return f"{self.goal_id}@{self.revision}:{self.contract_sha256}"


_CANONICAL_GOAL_TYPE: Final = EconomicGoalContract
_CANONICAL_GOAL_VALIDATOR: Final = EconomicGoalContract.__post_init__
_CANONICAL_PROVENANCE_TYPE: Final = EconomicGoalProvenance
_CANONICAL_PROVENANCE_VALIDATOR: Final = EconomicGoalProvenance.__post_init__


def _snapshot_provenance(
    provenance: EconomicGoalProvenance,
    _provenance_type=_CANONICAL_PROVENANCE_TYPE,
    _validator=_CANONICAL_PROVENANCE_VALIDATOR,
    _error_type=EconomicGoalProvenanceError,
) -> EconomicGoalProvenance:
    if type(provenance) is not _provenance_type:
        raise _error_type("provenance must be EconomicGoalProvenance")
    snapshot = _provenance_type(
        schema=provenance.schema,
        schema_version=provenance.schema_version,
        goal_id=provenance.goal_id,
        revision=provenance.revision,
        bankroll_id=provenance.bankroll_id,
        contract_sha256=provenance.contract_sha256,
    )
    _validator(snapshot)
    return snapshot


def _decision_identity_bound(
    self: EconomicGoalProvenance,
    _snapshotter=_snapshot_provenance,
) -> str:
    snapshot = _snapshotter(self)
    return (
        f"{snapshot.goal_id}@{snapshot.revision}:"
        f"{snapshot.contract_sha256}"
    )


EconomicGoalProvenance.decision_identity = property(_decision_identity_bound)


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
    _payload_encoder=economic_goal_to_payload,
    _payload_decoder=economic_goal_from_payload,
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
    payload = _payload_encoder(contract)
    snapshot = _payload_decoder(payload)
    provenance = _provenance_type(
        schema=_schema,
        schema_version=_schema_version,
        goal_id=snapshot.goal_id,
        revision=snapshot.revision,
        bankroll_id=snapshot.bankroll_id,
        contract_sha256=_contract_sha256(snapshot),
    )
    _provenance_validator(provenance)
    return provenance


def _verify_provenance_bound(
    contract: EconomicGoalContract,
    provenance: EconomicGoalProvenance,
    _goal_type=_CANONICAL_GOAL_TYPE,
    _provenance_type=_CANONICAL_PROVENANCE_TYPE,
    _goal_validator=_CANONICAL_GOAL_VALIDATOR,
    _payload_encoder=economic_goal_to_payload,
    _payload_decoder=economic_goal_from_payload,
    _provenance_snapshotter=_snapshot_provenance,
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
    contract_snapshot = _payload_decoder(_payload_encoder(contract))
    evidence = _provenance_snapshotter(provenance)
    if evidence.goal_id != contract_snapshot.goal_id:
        raise _provenance_error("provenance goal_id mismatch")
    if evidence.revision != contract_snapshot.revision:
        raise _provenance_error("provenance revision mismatch")
    if evidence.bankroll_id != contract_snapshot.bankroll_id:
        raise _provenance_error("provenance bankroll_id mismatch")
    actual = _contract_sha256(contract_snapshot)
    if evidence.contract_sha256 != actual:
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
