"""Seal live decision policy and reevaluation dispatch to canonical ledger authority.

This is a composition guard over the existing live-decision, reevaluation, and Decision
Ledger implementations. It does not create a second ledger, policy engine, or decision
resolver. Its only authority is to reject caller-polymorphic or shadowed ledger dispatch
before the existing product-owned resolvers can use it.
"""

from __future__ import annotations

from types import MethodType

from . import decision_ledger as _ledger_module
from . import live_decision_disposition as _target
from . import live_decision_reevaluation as _reevaluation
from .decision_ledger import DecisionRecord, EconomicDecisionAuthority, JsonlDecisionLedger
from .portfolio_plan import PortfolioPlan


_ORIGINAL_RESOLVER = _target._resolve_product_policy_binding
_ORIGINAL_RESOLVER_CODE = _ORIGINAL_RESOLVER.__code__
_ORIGINAL_VERIFIED_RECORDS_OR_PRISTINE = _reevaluation._verified_records_or_pristine
_ORIGINAL_VERIFIED_RECORDS_OR_PRISTINE_CODE = (
    _ORIGINAL_VERIFIED_RECORDS_OR_PRISTINE.__code__
)
_CANONICAL_PROVENANCE_FOR = _target.provenance_for
_CANONICAL_PROVENANCE_FOR_CODE = _CANONICAL_PROVENANCE_FOR.__code__
_CANONICAL_VERIFY_ECONOMIC_GOAL_BINDING = _ledger_module.verify_economic_goal_binding
_CANONICAL_VERIFY_ECONOMIC_GOAL_BINDING_CODE = (
    _CANONICAL_VERIFY_ECONOMIC_GOAL_BINDING.__code__
)
_CANONICAL_DECISION_RECORD_TO_DICT = DecisionRecord.to_dict
_CANONICAL_DECISION_RECORD_TO_DICT_CODE = _CANONICAL_DECISION_RECORD_TO_DICT.__code__
_CANONICAL_PORTFOLIO_FROM_DICT = PortfolioPlan.from_dict.__func__
_CANONICAL_PORTFOLIO_FROM_DICT_CODE = _CANONICAL_PORTFOLIO_FROM_DICT.__code__
_CANONICAL_DISPOSITION_FROM_JSON = _target.LiveDecisionDisposition.from_json.__func__
_CANONICAL_DISPOSITION_FROM_JSON_CODE = _CANONICAL_DISPOSITION_FROM_JSON.__code__
_CANONICAL_REEVALUATION_POLICY_VERIFY = _reevaluation.verify_product_policy_authority
_CANONICAL_REEVALUATION_POLICY_VERIFY_CODE = _CANONICAL_REEVALUATION_POLICY_VERIFY.__code__

# Every method in this chain is reached by the canonical economic-decision or
# reevaluation read/write. An exact ledger instance still has a writable __dict__, so
# instance shadowing is rejected separately from class-level rebinding. Function-object
# identity is not sufficient in Python because callers can replace ``__code__`` in place;
# retain the original executable witness too.
_LEDGER_SURFACES = {
    "append": (JsonlDecisionLedger.append, JsonlDecisionLedger.append.__code__),
    "_append_validated": (
        JsonlDecisionLedger._append_validated,
        JsonlDecisionLedger._append_validated.__code__,
    ),
    "verified_economic_decision": (
        JsonlDecisionLedger.verified_economic_decision,
        JsonlDecisionLedger.verified_economic_decision.__code__,
    ),
    "verified_records": (
        JsonlDecisionLedger.verified_records,
        JsonlDecisionLedger.verified_records.__code__,
    ),
    "verified_snapshot": (
        JsonlDecisionLedger.verified_snapshot,
        JsonlDecisionLedger.verified_snapshot.__code__,
    ),
    "_verify_bytes": (
        JsonlDecisionLedger._verify_bytes.__func__,
        JsonlDecisionLedger._verify_bytes.__func__.__code__,
    ),
    "_validate_record": (
        JsonlDecisionLedger._validate_record.__func__,
        JsonlDecisionLedger._validate_record.__func__.__code__,
    ),
    "_validate_json_value": (
        JsonlDecisionLedger._validate_json_value.__func__,
        JsonlDecisionLedger._validate_json_value.__func__.__code__,
    ),
    "_canonical_record": (
        JsonlDecisionLedger._canonical_record,
        JsonlDecisionLedger._canonical_record.__code__,
    ),
    "_json_object_without_duplicate_keys": (
        JsonlDecisionLedger._json_object_without_duplicate_keys,
        JsonlDecisionLedger._json_object_without_duplicate_keys.__code__,
    ),
    "_reject_non_finite_json": (
        JsonlDecisionLedger._reject_non_finite_json,
        JsonlDecisionLedger._reject_non_finite_json.__code__,
    ),
    "_require_utf8_text": (
        JsonlDecisionLedger._require_utf8_text,
        JsonlDecisionLedger._require_utf8_text.__code__,
    ),
}


def _surface_target(owner: type[JsonlDecisionLedger], name: str):
    value = getattr(owner, name)
    if isinstance(value, MethodType):
        return value.__func__
    return value


def _unchanged(current, expected, expected_code) -> bool:
    return current is expected and getattr(expected, "__code__", None) is expected_code


def _dispatch_is_canonical(ledger: JsonlDecisionLedger) -> bool:
    if type(ledger) is not JsonlDecisionLedger:
        return False
    instance_state = vars(ledger)
    for name, (expected, expected_code) in _LEDGER_SURFACES.items():
        current = _surface_target(JsonlDecisionLedger, name)
        if name in instance_state or not _unchanged(current, expected, expected_code):
            return False
    return (
        _unchanged(
            _ledger_module.verify_economic_goal_binding,
            _CANONICAL_VERIFY_ECONOMIC_GOAL_BINDING,
            _CANONICAL_VERIFY_ECONOMIC_GOAL_BINDING_CODE,
        )
        and _unchanged(
            DecisionRecord.to_dict,
            _CANONICAL_DECISION_RECORD_TO_DICT,
            _CANONICAL_DECISION_RECORD_TO_DICT_CODE,
        )
        and _unchanged(
            PortfolioPlan.from_dict.__func__,
            _CANONICAL_PORTFOLIO_FROM_DICT,
            _CANONICAL_PORTFOLIO_FROM_DICT_CODE,
        )
        and _unchanged(
            _target.LiveDecisionDisposition.from_json.__func__,
            _CANONICAL_DISPOSITION_FROM_JSON,
            _CANONICAL_DISPOSITION_FROM_JSON_CODE,
        )
        and _unchanged(
            _target.provenance_for,
            _CANONICAL_PROVENANCE_FOR,
            _CANONICAL_PROVENANCE_FOR_CODE,
        )
        and _unchanged(
            _reevaluation.verify_product_policy_authority,
            _CANONICAL_REEVALUATION_POLICY_VERIFY,
            _CANONICAL_REEVALUATION_POLICY_VERIFY_CODE,
        )
    )


def _guarded_resolver(*, ledger, authority, decision_id):
    if type(authority) is not EconomicDecisionAuthority:
        raise TypeError("authority must be exact EconomicDecisionAuthority")
    if not _dispatch_is_canonical(ledger):
        raise TypeError("ledger must expose canonical JsonlDecisionLedger authority")
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


def _guarded_verified_records_or_pristine(ledger):
    if not _dispatch_is_canonical(ledger):
        raise TypeError("ledger must expose canonical JsonlDecisionLedger authority")
    if (
        _reevaluation._verified_records_or_pristine
        is not _guarded_verified_records_or_pristine
        or _ORIGINAL_VERIFIED_RECORDS_OR_PRISTINE.__code__
        is not _ORIGINAL_VERIFIED_RECORDS_OR_PRISTINE_CODE
    ):
        raise _target.LiveDecisionDispositionError(
            "reevaluation durable-read authority changed"
        )
    records = _ORIGINAL_VERIFIED_RECORDS_OR_PRISTINE(ledger)
    if not _dispatch_is_canonical(ledger):
        raise _target.LiveDecisionDispositionError(
            "reevaluation ledger authority changed during durable read"
        )
    return records


_target._resolve_product_policy_binding = _guarded_resolver
_reevaluation._verified_records_or_pristine = _guarded_verified_records_or_pristine
