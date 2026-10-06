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
from .economic_goal_store import economic_goal_from_payload, economic_goal_to_payload


PROVENANCE_SCHEMA: Final = "autosport.economic_goal_provenance"
PROVENANCE_SCHEMA_VERSION: Final = 1
_MAX_PROVENANCE_IDENTITY_CHARS: Final = 512


class _EconomicGoalProvenanceMeta(type):
    """Seal the public provenance-identity property after canonical binding."""

    _AUTHORITY_NAMES: Final = frozenset(
        {
            "decision_identity",
            "_authority_operations_sealed",
        }
    )

    def __setattr__(
        cls,
        name: str,
        value: object,
        _authority_names=_AUTHORITY_NAMES,
    ) -> None:
        if (
            cls.__dict__.get("_authority_operations_sealed", False)
            and name in _authority_names
        ):
            raise TypeError(
                "economic-goal provenance public authority binding is immutable"
            )
        super().__setattr__(name, value)

    def __delattr__(
        cls,
        name: str,
        _authority_names=_AUTHORITY_NAMES,
    ) -> None:
        if (
            cls.__dict__.get("_authority_operations_sealed", False)
            and name in _authority_names
        ):
            raise TypeError(
                "economic-goal provenance public authority binding is immutable"
            )
        super().__delattr__(name)



def _build_provenance_class_guard(name: str):
    """Block direct base-metaclass mutation of sealed provenance authority names."""

    class _ProvenanceClassGuard:
        __slots__ = ()

        def __get__(self, instance, owner=None):
            if instance is None:
                return self
            for ancestor in instance.__mro__:
                if name in ancestor.__dict__:
                    binding = ancestor.__dict__[name]
                    break
            else:
                raise AttributeError(name)
            descriptor_get = getattr(binding, "__get__", None)
            if descriptor_get is None:
                return binding
            return descriptor_get(None, instance)

        def __set__(self, _instance, _value) -> None:
            raise TypeError(
                "economic-goal provenance public authority binding is immutable"
            )

        def __delete__(self, _instance) -> None:
            raise TypeError(
                "economic-goal provenance public authority binding is immutable"
            )

    return _ProvenanceClassGuard()


class EconomicGoalProvenanceError(ValueError):
    """Raised when economic-goal provenance evidence is malformed or mismatched."""


@dataclass(frozen=True, slots=True)
class EconomicGoalProvenance(metaclass=_EconomicGoalProvenanceMeta):
    """Immutable evidence identity for one persisted goal-contract revision."""

    schema: str
    schema_version: int
    goal_id: str
    revision: int
    bankroll_id: str
    contract_sha256: str

    _authority_operations_sealed = False

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
    _provenance_type=EconomicGoalProvenance,
) -> tuple[object, ...]:
    return tuple(
        getter(provenance, _provenance_type)
        for _, getter in _field_getters
    )

def _validate_provenance_bound(
    self: EconomicGoalProvenance,
    _schema=PROVENANCE_SCHEMA,
    _schema_version=PROVENANCE_SCHEMA_VERSION,
    _max_identity_chars=_MAX_PROVENANCE_IDENTITY_CHARS,
    _error_type=EconomicGoalProvenanceError,
    _provenance_type=EconomicGoalProvenance,
    _snapshot=_canonical_provenance_snapshot,
) -> None:
    """Validate provenance through captured slot descriptors."""
    if type(self) is not _provenance_type:
        raise _error_type("provenance must use the exact evidence type")
    schema, schema_version, goal_id, revision, bankroll_id, contract_sha256 = _snapshot(self)
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

def _capture_callable_authority_graph(root):
    """Capture every callable reachable through function defaults, cycle-safely."""

    captured = []
    seen: set[int] = set()

    def visit(candidate) -> None:
        if not callable(candidate):
            return
        identity = id(candidate)
        if identity in seen:
            return
        seen.add(identity)
        defaults = getattr(candidate, "__defaults__", None)
        kwdefaults = getattr(candidate, "__kwdefaults__", None)
        kwdefault_items = tuple((kwdefaults or {}).items())
        captured.append(
            (
                candidate,
                getattr(candidate, "__code__", None),
                defaults,
                kwdefaults,
                kwdefault_items,
            )
        )
        for value in defaults or ():
            if callable(value):
                visit(value)
        for _, value in kwdefault_items:
            if callable(value):
                visit(value)

    visit(root)
    return tuple(captured)


def _make_provenance_post_init_authority(operation):
    authority_graph = _capture_callable_authority_graph(operation)
    error_type = EconomicGoalProvenanceError

    def require_authority() -> None:
        for index, (
            callable_object,
            expected_code,
            expected_defaults,
            expected_kwdefaults,
            expected_kwdefault_items,
        ) in enumerate(authority_graph):
            if getattr(callable_object, "__code__", None) is not expected_code:
                if index == 0:
                    raise error_type("economic-goal provenance validator authority changed")
                raise error_type("economic-goal provenance nested validator authority changed")
            if getattr(callable_object, "__defaults__", None) is not expected_defaults:
                if index == 0:
                    raise error_type("economic-goal provenance validator defaults authority changed")
                raise error_type("economic-goal provenance nested validator defaults authority changed")
            current_kwdefaults = getattr(callable_object, "__kwdefaults__", None)
            if (
                current_kwdefaults is not expected_kwdefaults
                or tuple((current_kwdefaults or {}).items()) != expected_kwdefault_items
            ):
                if index == 0:
                    raise error_type(
                        "economic-goal provenance validator keyword defaults authority changed"
                    )
                raise error_type(
                    "economic-goal provenance nested validator keyword defaults authority changed"
                )

    def bound(self: EconomicGoalProvenance) -> None:
        require_authority()
        operation(self)
        require_authority()

    return bound

_CANONICAL_GOAL_TYPE: Final = EconomicGoalContract
_CANONICAL_GOAL_VALIDATOR: Final = EconomicGoalContract.__post_init__
_CANONICAL_PROVENANCE_TYPE: Final = EconomicGoalProvenance
_CANONICAL_PROVENANCE_VALIDATOR: Final = _make_provenance_post_init_authority(
    _validate_provenance_bound
)
EconomicGoalProvenance.__post_init__ = _CANONICAL_PROVENANCE_VALIDATOR


def _provenance_init_authority(
    self: EconomicGoalProvenance,
    schema: str,
    schema_version: int,
    goal_id: str,
    revision: int,
    bankroll_id: str,
    contract_sha256: str,
    _validator=_CANONICAL_PROVENANCE_VALIDATOR,
    _setattr=_PROVENANCE_OBJECT_SETATTR,
) -> None:
    for name, value in (
        ("schema", schema),
        ("schema_version", schema_version),
        ("goal_id", goal_id),
        ("revision", revision),
        ("bankroll_id", bankroll_id),
        ("contract_sha256", contract_sha256),
    ):
        _setattr(self, name, value)
    _validator(self)


def _make_provenance_constructor_authority(operation):
    authority_graph = _capture_callable_authority_graph(operation)
    error_type = EconomicGoalProvenanceError

    def require_authority() -> None:
        for index, (
            callable_object,
            expected_code,
            expected_defaults,
            expected_kwdefaults,
            expected_kwdefault_items,
        ) in enumerate(authority_graph):
            if getattr(callable_object, "__code__", None) is not expected_code:
                if index == 0:
                    raise error_type("economic-goal provenance constructor authority changed")
                raise error_type("economic-goal provenance constructor nested authority changed")
            if getattr(callable_object, "__defaults__", None) is not expected_defaults:
                if index == 0:
                    raise error_type(
                        "economic-goal provenance constructor defaults authority changed"
                    )
                raise error_type(
                    "economic-goal provenance constructor nested defaults authority changed"
                )
            current_kwdefaults = getattr(callable_object, "__kwdefaults__", None)
            if (
                current_kwdefaults is not expected_kwdefaults
                or tuple((current_kwdefaults or {}).items()) != expected_kwdefault_items
            ):
                if index == 0:
                    raise error_type(
                        "economic-goal provenance constructor keyword defaults authority changed"
                    )
                raise error_type(
                    "economic-goal provenance constructor nested keyword defaults authority changed"
                )

    def bound(*args, **kwargs):
        require_authority()
        result = operation(*args, **kwargs)
        require_authority()
        return result

    return bound

def _bind_provenance_constructor(operation):
    bound_operation = _make_provenance_constructor_authority(operation)

    def bound(
        self: EconomicGoalProvenance,
        schema: str,
        schema_version: int,
        goal_id: str,
        revision: int,
        bankroll_id: str,
        contract_sha256: str,
    ) -> None:
        bound_operation(
            self,
            schema,
            schema_version,
            goal_id,
            revision,
            bankroll_id,
            contract_sha256,
        )

    return bound


_CANONICAL_PROVENANCE_INIT: Final = _bind_provenance_constructor(
    _provenance_init_authority
)


def _build_provenance(
    *,
    schema: str,
    schema_version: int,
    goal_id: str,
    revision: int,
    bankroll_id: str,
    contract_sha256: str,
    _provenance_type=_CANONICAL_PROVENANCE_TYPE,
    _validator=_CANONICAL_PROVENANCE_VALIDATOR,
    _object_new=object.__new__,
    _object_setattr=object.__setattr__,
) -> EconomicGoalProvenance:
    evidence = _object_new(_provenance_type)
    for name, value in (
        ("schema", schema),
        ("schema_version", schema_version),
        ("goal_id", goal_id),
        ("revision", revision),
        ("bankroll_id", bankroll_id),
        ("contract_sha256", contract_sha256),
    ):
        _object_setattr(evidence, name, value)
    _validator(evidence)
    return evidence


def _snapshot_provenance(
    provenance: EconomicGoalProvenance,
    _provenance_type=_CANONICAL_PROVENANCE_TYPE,
    _builder=_build_provenance,
    _error_type=EconomicGoalProvenanceError,
    _snapshot=_canonical_provenance_snapshot,
) -> EconomicGoalProvenance:
    if type(provenance) is not _provenance_type:
        raise _error_type("provenance must be EconomicGoalProvenance")
    values = _snapshot(provenance)
    return _builder(
        schema=values[0],
        schema_version=values[1],
        goal_id=values[2],
        revision=values[3],
        bankroll_id=values[4],
        contract_sha256=values[5],
    )


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
    _provenance_builder=_build_provenance,
    _provenance_validator=_CANONICAL_PROVENANCE_VALIDATOR,
    _contract_sha256=_contract_sha256_bound,
    _schema=PROVENANCE_SCHEMA,
    _schema_version=PROVENANCE_SCHEMA_VERSION,
    _goal_error=EconomicGoalContractError,
    _contract_snapshot=_canonical_contract_snapshot,
    _contract_field_names=_PROVENANCE_CONTRACT_FIELD_NAMES,
) -> EconomicGoalProvenance:
    """Derive immutable provenance identity without introducing another authority."""

    if type(contract) is not _goal_type:
        raise _goal_error("provenance requires an EconomicGoalContract")
    _goal_validator(contract)
    before = _contract_snapshot(contract)
    _goal_validator(contract)
    after = _contract_snapshot(contract)
    if before != after:
        raise _goal_error("economic goal changed during provenance derivation")
    values = dict(zip(_contract_field_names, after))
    contract_sha256 = _contract_sha256(contract)
    final_snapshot = _contract_snapshot(contract)
    if after != final_snapshot:
        raise _goal_error("economic goal changed during provenance derivation")
    provenance = _provenance_builder(
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
    _payload_encoder=economic_goal_to_payload,
    _payload_decoder=economic_goal_from_payload,
    _provenance_snapshotter=_snapshot_provenance,
    _provenance_validator=_CANONICAL_PROVENANCE_VALIDATOR,
    _contract_sha256=_contract_sha256_bound,
    _goal_error=EconomicGoalContractError,
    _provenance_error=EconomicGoalProvenanceError,
    _contract_snapshot=_canonical_contract_snapshot,
    _provenance_snapshot=_canonical_provenance_snapshot,
) -> None:
    """Fail closed when provenance no longer matches the canonical contract."""

    if type(contract) is not _goal_type:
        raise _goal_error("provenance verification requires an EconomicGoalContract")
    if type(provenance) is not _provenance_type:
        raise _provenance_error("provenance must be EconomicGoalProvenance")

    _goal_validator(contract)
    contract_before = _contract_snapshot(contract)
    _provenance_validator(provenance)
    provenance_before = _provenance_snapshot(provenance)

    _goal_validator(contract)
    contract_after = _contract_snapshot(contract)
    _provenance_validator(provenance)
    provenance_after = _provenance_snapshot(provenance)

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
    final_contract_snapshot = _contract_snapshot(contract)
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


def _decision_identity_bound(
    self: EconomicGoalProvenance,
    _validator=_CANONICAL_PROVENANCE_VALIDATOR,
    _snapshot=_canonical_provenance_snapshot,
    _error_type=EconomicGoalProvenanceError,
) -> str:
    """Derive identity from two coherent reads of the bound provenance state."""
    _validator(self)
    before = _snapshot(self)
    _validator(self)
    after = _snapshot(self)
    if before != after:
        raise _error_type(
            "economic-goal provenance changed during identity derivation"
        )
    _, _, goal_id, revision, _, contract_sha256 = after
    return f"{goal_id}@{revision}:{contract_sha256}"


def _make_provenance_authority(operation, label: str):
    authority_graph = _capture_callable_authority_graph(operation)

    def require_authority() -> None:
        for index, (
            callable_object,
            expected_code,
            expected_defaults,
            expected_kwdefaults,
            expected_kwdefault_items,
        ) in enumerate(authority_graph):
            suffix = "" if index == 0 else " nested"
            if getattr(callable_object, "__code__", None) is not expected_code:
                raise EconomicGoalProvenanceError(
                    f"{label}{suffix} authority changed"
                )
            if getattr(callable_object, "__defaults__", None) is not expected_defaults:
                raise EconomicGoalProvenanceError(
                    f"{label}{suffix} defaults authority changed"
                )
            current_kwdefaults = getattr(callable_object, "__kwdefaults__", None)
            if (
                current_kwdefaults is not expected_kwdefaults
                or tuple((current_kwdefaults or {}).items()) != expected_kwdefault_items
            ):
                raise EconomicGoalProvenanceError(
                    f"{label}{suffix} keyword defaults authority changed"
                )

    def bound(*args, **kwargs):
        require_authority()
        result = operation(*args, **kwargs)
        require_authority()
        return result

    return bound

# Public authority-bearing operations intentionally expose no injectable helper
# parameters.  The closures capture the already-bound canonical implementations,
# so rebinding module aliases or callable defaults cannot redirect dispatch.
def _bind_contract_operation(operation):
    bound_operation = _make_provenance_authority(operation, "economic-goal provenance operation")

    def bound(contract: EconomicGoalContract):
        return bound_operation(contract)

    return bound


def _bind_provenance_verifier(operation):
    bound_operation = _make_provenance_authority(operation, "economic-goal provenance verification")

    def bound(
        contract: EconomicGoalContract,
        provenance: EconomicGoalProvenance,
    ) -> None:
        bound_operation(contract, provenance)

    return bound


contract_sha256 = _bind_contract_operation(_contract_sha256_bound)
provenance_for = _bind_contract_operation(_provenance_for_bound)
verify_provenance = _bind_provenance_verifier(_verify_provenance_bound)

_decision_identity_authority = _make_provenance_authority(
    _decision_identity_bound,
    "economic-goal provenance decision identity",
)
EconomicGoalProvenance.decision_identity = property(_decision_identity_authority)

# Freeze the public identity property after its closure has captured canonical authority.
EconomicGoalProvenance._authority_operations_sealed = True

for _sealed_provenance_name in _EconomicGoalProvenanceMeta._AUTHORITY_NAMES:
    setattr(
        _EconomicGoalProvenanceMeta,
        _sealed_provenance_name,
        _build_provenance_class_guard(_sealed_provenance_name),
    )
del _sealed_provenance_name
