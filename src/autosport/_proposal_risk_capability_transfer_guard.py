"""Prevent transfer of proposal-risk identity capabilities between exact DTOs.

The owning authority modules keep identity tokens out of their mutable module
namespaces, but the dataclass capability slots are still Python attributes. A caller
that already holds one valid DTO must not be able to read its token and install that
token on an ``object.__new__`` forged instance with ``object.__setattr__``.

This package composition keeps the existing authority format unchanged. It wraps only
the hidden capability member descriptors and permits their one canonical first write
when the exact product issuance call chain is executing. Durable/business fields and
all positive authority semantics remain owned by the existing modules.
"""
from __future__ import annotations

import sys
from types import FunctionType

from . import proposal_risk_evaluation_precommit_authority as _precommit
from . import proposal_risk_execution_evidence_authority as _execution

_GETFRAME = sys._getframe


class ProposalRiskCapabilityTransferGuardError(RuntimeError):
    """A proposal-risk identity capability escaped its canonical mint path."""


def _closure_value(function: FunctionType, name: str) -> object:
    if type(function) is not FunctionType:
        raise RuntimeError("proposal-risk sealed function is unavailable")
    closure = function.__closure__ or ()
    values = dict(zip(function.__code__.co_freevars, closure, strict=True))
    cell = values.get(name)
    if cell is None:
        raise RuntimeError(f"proposal-risk sealed closure {name!r} is unavailable")
    return cell.cell_contents


def _code(function: object, label: str):
    if type(function) is not FunctionType:
        raise RuntimeError(f"proposal-risk {label} function is unavailable")
    code = function.__code__
    if code is None:
        raise RuntimeError(f"proposal-risk {label} code is unavailable")
    return code


def _make_canonical_bind_descriptor(slot, field_name: str, allowed_chains):
    if slot is None or not hasattr(slot, "__get__") or not hasattr(slot, "__set__"):
        raise RuntimeError(f"proposal-risk capability slot {field_name} is unavailable")
    chains = tuple(tuple(chain) for chain in allowed_chains)
    getframe = _GETFRAME

    def canonical_call_chain() -> bool:
        for chain in chains:
            frame = getframe(2)
            matched = True
            for expected_code in chain:
                if frame is None or frame.f_code is not expected_code:
                    matched = False
                    break
                frame = frame.f_back
            if matched:
                return True
        return False

    class _CanonicalBindDescriptor:
        __slots__ = ()
        _autosport_proposal_risk_capability_gate = True

        def __get__(self, instance, owner=None):
            if instance is None:
                return self
            return slot.__get__(instance, owner)

        def __set__(self, instance, value) -> None:
            if not canonical_call_chain():
                raise AttributeError(
                    f"{field_name} may be bound only by canonical product issuance"
                )
            try:
                slot.__get__(instance, type(instance))
            except AttributeError:
                slot.__set__(instance, value)
                return
            raise AttributeError(f"{field_name} is already product-bound")

        def __delete__(self, instance) -> None:
            raise AttributeError(f"{field_name} is product-bound and not deletable")

    return _CanonicalBindDescriptor()


def _install_precommit_capability_gate() -> None:
    owner = _precommit.ProductProposalRiskEvaluationPrecommit
    field_name = "_proposal_risk_evaluation_precommit_capability"
    slot = owner.__dict__.get(field_name)
    if getattr(slot, "_autosport_proposal_risk_capability_gate", False) is True:
        return

    build = _precommit._build
    build_defaults = build.__kwdefaults__
    if type(build_defaults) is not dict or type(build_defaults.get("_bind")) is not FunctionType:
        raise RuntimeError("proposal-risk precommit canonical binder is unavailable")
    bind = build_defaults["_bind"]

    sealed_issue = _precommit.issue_product_proposal_risk_evaluation_precommit
    sealed_resolve = _precommit.resolve_product_proposal_risk_evaluation_precommit
    canonical_issue = _closure_value(sealed_issue, "canonical_issue")
    canonical_resolve = _closure_value(sealed_resolve, "canonical_resolve")

    setattr(
        owner,
        field_name,
        _make_canonical_bind_descriptor(
            slot,
            field_name,
            (
                (
                    _code(bind, "precommit bind"),
                    _code(build, "precommit build"),
                    _code(canonical_issue, "canonical precommit issue"),
                    _code(sealed_issue, "sealed precommit issue"),
                ),
                (
                    _code(bind, "precommit bind"),
                    _code(build, "precommit build"),
                    _code(canonical_resolve, "canonical precommit resolve"),
                    _code(sealed_resolve, "sealed precommit resolve"),
                ),
            ),
        ),
    )


def _install_execution_result_capability_gate() -> None:
    owner = _execution.ProductProposalRiskExecutionEvidence
    field_name = "_execution_evidence_capability"
    slot = owner.__dict__.get(field_name)
    if getattr(slot, "_autosport_proposal_risk_capability_gate", False) is True:
        return

    sealed_mint = _execution._mint
    canonical_mint = _closure_value(sealed_mint, "canonical_mint")
    mint_defaults = canonical_mint.__kwdefaults__
    if (
        type(mint_defaults) is not dict
        or type(mint_defaults.get("_bind_identity")) is not FunctionType
    ):
        raise RuntimeError("proposal-risk execution canonical identity binder is unavailable")
    bind_identity = mint_defaults["_bind_identity"]

    sealed_derive = _execution.derive_product_proposal_risk_execution_evidence
    canonical_derive = _closure_value(sealed_derive, "canonical_derive")

    setattr(
        owner,
        field_name,
        _make_canonical_bind_descriptor(
            slot,
            field_name,
            (
                (
                    _code(bind_identity, "execution identity bind"),
                    _code(canonical_mint, "canonical execution mint"),
                    _code(sealed_mint, "sealed execution mint"),
                    _code(canonical_derive, "canonical execution derive"),
                    _code(sealed_derive, "sealed execution derive"),
                ),
            ),
        ),
    )


_install_precommit_capability_gate()
_install_execution_result_capability_gate()

del _install_precommit_capability_gate
del _install_execution_result_capability_gate
del _make_canonical_bind_descriptor
del _closure_value
del _code
del _GETFRAME
del sys
del FunctionType
del _precommit
del _execution
