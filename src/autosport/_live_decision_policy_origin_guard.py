"""Seal live decision policy re-resolution to canonical ledger authority.

This is a composition guard over the existing live-decision and Decision Ledger
implementations. It does not create a second ledger, policy engine, or decision
resolver. Its only authority is to reject caller-polymorphic or shadowed read
dispatch before the existing resolver can use it.
"""

from __future__ import annotations

from types import MethodType

from . import decision_ledger as _ledger_module
from . import live_decision_disposition as _target
from .decision_ledger import DecisionRecord, EconomicDecisionAuthority, JsonlDecisionLedger
from .portfolio_plan import PortfolioPlan


_ORIGINAL_RESOLVER = _target._resolve_product_policy_binding
_ORIGINAL_RESOLVER_CODE = _ORIGINAL_RESOLVER.__code__
_CANONICAL_PROVENANCE_FOR = _target.provenance_for
_CANONICAL_VERIFY_ECONOMIC_GOAL_BINDING = _ledger_module.verify_economic_goal_binding
_CANONICAL_DECISION_RECORD_TO_DICT = DecisionRecord.to_dict
_CANONICAL_PORTFOLIO_FROM_DICT = PortfolioPlan.from_dict.__func__

# Every method in this chain is reached by the canonical economic-decision read.
# An exact ledger instance still has a writable __dict__, so instance shadowing is
# rejected separately from class-level rebinding.
_LEDGER_SURFACES = {
    "verified_economic_decision": JsonlDecisionLedger.verified_economic_decision,
    "verified_records": JsonlDecisionLedger.verified_records,
    "verified_snapshot": JsonlDecisionLedger.verified_snapshot,
    "_verify_bytes": JsonlDecisionLedger._verify_bytes.__func__,
    "_validate_record": JsonlDecisionLedger._validate_record.__func__,
    "_validate_json_value": JsonlDecisionLedger._validate_json_value.__func__,
    "_canonical_record": JsonlDecisionLedger._canonical_record,
    "_json_object_without_duplicate_keys": JsonlDecisionLedger._json_object_without_duplicate_keys,
    "_reject_non_finite_json": JsonlDecisionLedger._reject_non_finite_json,
    "_require_utf8_text": JsonlDecisionLedger._require_utf8_text,
}


def _surface_target(owner: type[JsonlDecisionLedger], name: str):
    value = getattr(owner, name)
    if isinstance(value, MethodType):
        return value.__func__
    return value


def _dispatch_is_canonical(ledger: JsonlDecisionLedger) -> bool:
    if type(ledger) is not JsonlDecisionLedger:
        return False
    instance_state = vars(ledger)
    for name, expected in _LEDGER_SURFACES.items():
        if name in instance_state or _surface_target(JsonlDecisionLedger, name) is not expected:
            return False
    return (
        _ledger_module.verify_economic_goal_binding
        is _CANONICAL_VERIFY_ECONOMIC_GOAL_BINDING
        and DecisionRecord.to_dict is _CANONICAL_DECISION_RECORD_TO_DICT
        and PortfolioPlan.from_dict.__func__ is _CANONICAL_PORTFOLIO_FROM_DICT
        and _target.provenance_for is _CANONICAL_PROVENANCE_FOR
    )


def _guarded_resolver(*, ledger, authority, decision_id):
    if type(authority) is not EconomicDecisionAuthority:
        raise TypeError("authority must be exact EconomicDecisionAuthority")
    if not _dispatch_is_canonical(ledger):
        raise TypeError("ledger must expose canonical JsonlDecisionLedger read authority")
    if (
        _target._resolve_product_policy_binding is not _guarded_resolver
        or _ORIGINAL_RESOLVER.__code__ is not _ORIGINAL_RESOLVER_CODE
    ):
        raise _target.LiveDecisionDispositionError(
            "product policy resolver authority changed"
        )
    result = _ORIGINAL_RESOLVER(
        ledger=ledger,
        authority=authority,
        decision_id=decision_id,
    )
    if not _dispatch_is_canonical(ledger):
        raise _target.LiveDecisionDispositionError(
            "product policy ledger authority changed during re-resolution"
        )
    return result


_target._resolve_product_policy_binding = _guarded_resolver
