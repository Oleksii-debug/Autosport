from __future__ import annotations

from dataclasses import fields

import pytest

import autosport.betfair_standard_limit_price_bound_verifier as verifier_module
import autosport.supervised_plan_issuance as issuance_module
from autosport.betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    BetfairStandardLimitPriceBoundEvidence,
)
from autosport.real_execution_ledger import RealExecutionLedger

from test_betfair_supervised_execution import _bound, _profile
from test_supervised_plan_issuance import _store


def _copy_with_instruction_sha(
    evidence: BetfairStandardLimitPriceBoundEvidence,
    instruction_sha256: str,
) -> BetfairStandardLimitPriceBoundEvidence:
    forged = object.__new__(BetfairStandardLimitPriceBoundEvidence)
    for field in fields(BetfairStandardLimitPriceBoundEvidence):
        object.__setattr__(forged, field.name, getattr(evidence, field.name))
    object.__setattr__(forged, "instruction_sha256", instruction_sha256)
    return forged


def test_coordinated_preissuance_resolver_substitution_cannot_mint_provider_authority(
    monkeypatch,
    tmp_path,
) -> None:
    bound, approval, _goal = _bound(_profile())
    store = _store(monkeypatch, tmp_path, bound)
    canonical_resolver = issuance_module.resolve_betfair_standard_limit_price_bound
    forged_sha = "f" * 64

    def hostile_resolver(*, bound, action_id):
        genuine = canonical_resolver(bound=bound, action_id=action_id)
        return _copy_with_instruction_sha(genuine, forged_sha)

    # Reproduce the common-mode defect: the hostile resolver is present before
    # issuance and remains installed for durable reload.  The old verifier also
    # late-dispatched through its own mutable alias, so coordinating both aliases
    # made the same forged projection appear self-consistent end to end.
    monkeypatch.setattr(
        issuance_module,
        "resolve_betfair_standard_limit_price_bound",
        hostile_resolver,
    )
    monkeypatch.setattr(
        verifier_module,
        "resolve_betfair_standard_limit_price_bound",
        hostile_resolver,
    )

    issued = store.issue(bound=bound, approval=approval)
    assert issued.provider_requests[0]["instruction_sha256"] == forged_sha

    # Demonstrate that the issuance store alone is intentionally provider-neutral:
    # with the same hostile dispatch still installed, byte continuity/reload is
    # self-consistent.  Product provider authority must therefore come from the
    # independently sealed verifier re-resolution below.
    reloaded = store.load(bound.execution_plan.plan_id)
    assert reloaded.provider_requests == issued.provider_requests

    ledger = RealExecutionLedger(tmp_path / "execution-ledger.jsonl")
    ledger.reserve_plan(bound.execution_plan)
    ledger.bind_supervised_approval(
        plan_id=bound.execution_plan.plan_id,
        approval_id=approval.ledger_identity,
        approval_fingerprint=approval.fingerprint,
        approved_at=approval.approved_at,
        evidence_sha256=approval.evidence_sha256,
    )
    candidate = hostile_resolver(
        bound=bound,
        action_id=bound.execution_plan.actions[0].action_id,
    )

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="durable issuance-time Betfair request identity changed",
    ):
        verifier_module.verify_betfair_standard_limit_price_bound(
            evidence=candidate,
            ledger=ledger,
            issuance_store=store,
            execution_plan_id=bound.execution_plan.plan_id,
            action_id=bound.execution_plan.actions[0].action_id,
        )


def test_in_place_canonical_resolver_code_swap_fails_before_fresh_authority(
    monkeypatch,
    tmp_path,
) -> None:
    bound, approval, _goal = _bound(_profile())
    store = _store(monkeypatch, tmp_path, bound)
    issued = store.issue(bound=bound, approval=approval)
    action = bound.execution_plan.actions[0]

    ledger = RealExecutionLedger(tmp_path / "execution-ledger.jsonl")
    ledger.reserve_plan(bound.execution_plan)
    ledger.bind_supervised_approval(
        plan_id=bound.execution_plan.plan_id,
        approval_id=approval.ledger_identity,
        approval_fingerprint=approval.fingerprint,
        approved_at=approval.approved_at,
        evidence_sha256=approval.evidence_sha256,
    )
    candidate = issuance_module.resolve_betfair_standard_limit_price_bound(
        bound=bound,
        action_id=action.action_id,
    )

    canonical = verifier_module._price_bound_module.resolve_betfair_standard_limit_price_bound

    def hostile_code(*, bound, action_id):
        del bound, action_id
        raise AssertionError("hostile resolver code must not execute")

    monkeypatch.setattr(canonical, "__code__", hostile_code.__code__)

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="canonical Betfair price-bound resolver authority changed",
    ):
        verifier_module.verify_betfair_standard_limit_price_bound(
            evidence=candidate,
            ledger=ledger,
            issuance_store=store,
            execution_plan_id=issued.bound.execution_plan.plan_id,
            action_id=action.action_id,
        )
