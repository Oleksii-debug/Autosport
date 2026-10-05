from __future__ import annotations

from pathlib import Path

import pytest

import autosport.supervised_plan_issuance as issuance_module

from autosport.betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    resolve_betfair_standard_limit_price_bound,
)
from autosport.betfair_standard_limit_price_bound_product_verifier import (
    verify_product_betfair_standard_limit_price_bound,
)
from autosport.real_execution_ledger import RealExecutionLedger
from autosport.supervised_plan_issuance import SupervisedPlanIssuanceStore

from test_supervised_plan_issuance import _active_runtime_profile, _issue


def _case(monkeypatch, tmp_path: Path):
    bound, approval, store, _issued = _issue(monkeypatch, tmp_path)
    action = bound.execution_plan.actions[0]
    ledger = RealExecutionLedger(store.workspace / "execution-ledger.jsonl")
    ledger.reserve_plan(bound.execution_plan)
    ledger.bind_supervised_approval(
        plan_id=bound.execution_plan.plan_id,
        approval_id=approval.ledger_identity,
        approval_fingerprint=approval.fingerprint,
        approved_at=approval.approved_at,
        evidence_sha256=approval.evidence_sha256,
    )
    evidence = resolve_betfair_standard_limit_price_bound(
        bound=bound,
        action_id=action.action_id,
    )
    return bound, action, store, ledger, evidence


def test_product_verifier_rejects_rebound_ledger_constructor_after_reopen(
    monkeypatch, tmp_path: Path
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)
    original_init = RealExecutionLedger.__init__
    attacker_called = False

    def attacker_init(self, path):
        nonlocal attacker_called
        attacker_called = True
        original_init(self, path)
        self.path = tmp_path / "attacker-ledger.jsonl"

    with _active_runtime_profile(store.workspace) as runtime_profile:
        monkeypatch.setattr(RealExecutionLedger, "__init__", attacker_init)
        with pytest.raises(
            BetfairStandardLimitPriceBoundError,
            match="product verifier reopen authority changed",
        ):
            verify_product_betfair_standard_limit_price_bound(
                evidence=evidence,
                ledger=ledger,
                issuance_store=store,
                runtime_profile=runtime_profile,
                execution_plan_id=bound.execution_plan.plan_id,
                action_id=action.action_id,
            )

    assert attacker_called is False


def test_product_verifier_rejects_rebound_store_constructor_after_reopen(
    monkeypatch, tmp_path: Path
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)
    original_init = SupervisedPlanIssuanceStore.__init__
    attacker_called = False

    def attacker_init(self, workspace, *args, **kwargs):
        nonlocal attacker_called
        attacker_called = True
        original_init(self, workspace, *args, **kwargs)
        self.workspace = tmp_path / "attacker-workspace"

    with _active_runtime_profile(store.workspace) as runtime_profile:
        monkeypatch.setattr(SupervisedPlanIssuanceStore, "__init__", attacker_init)
        with pytest.raises(
            BetfairStandardLimitPriceBoundError,
            match="product verifier reopen authority changed",
        ):
            verify_product_betfair_standard_limit_price_bound(
                evidence=evidence,
                ledger=ledger,
                issuance_store=store,
                runtime_profile=runtime_profile,
                execution_plan_id=bound.execution_plan.plan_id,
                action_id=action.action_id,
            )

    assert attacker_called is False

def test_product_verifier_rejects_in_place_ledger_constructor_code_mutation_before_execution(
    monkeypatch, tmp_path: Path,
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)
    constructor = RealExecutionLedger.__init__
    original_code = constructor.__code__
    attacker_called = False

    def attacker_init(self, path):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("mutated ledger constructor must never execute")

    try:
        constructor.__code__ = attacker_init.__code__
        with _active_runtime_profile(store.workspace) as runtime_profile:
            with pytest.raises(
                BetfairStandardLimitPriceBoundError,
                match="product verifier reopen authority changed",
            ):
                verify_product_betfair_standard_limit_price_bound(
                    evidence=evidence,
                    ledger=ledger,
                    issuance_store=store,
                    runtime_profile=runtime_profile,
                    execution_plan_id=bound.execution_plan.plan_id,
                    action_id=action.action_id,
                )
    finally:
        constructor.__code__ = original_code

    assert attacker_called is False


def test_product_verifier_rejects_in_place_store_constructor_code_mutation_before_execution(
    monkeypatch, tmp_path: Path,
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)
    constructor = SupervisedPlanIssuanceStore.__init__
    original_code = constructor.__code__
    attacker_called = False

    def attacker_init(self, workspace, *args, **kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("mutated store constructor must never execute")

    try:
        constructor.__code__ = attacker_init.__code__
        with _active_runtime_profile(store.workspace) as runtime_profile:
            with pytest.raises(
                BetfairStandardLimitPriceBoundError,
                match="product verifier reopen authority changed",
            ):
                verify_product_betfair_standard_limit_price_bound(
                    evidence=evidence,
                    ledger=ledger,
                    issuance_store=store,
                    runtime_profile=runtime_profile,
                    execution_plan_id=bound.execution_plan.plan_id,
                    action_id=action.action_id,
                )
    finally:
        constructor.__code__ = original_code

    assert attacker_called is False

def test_product_verifier_rejects_noncanonical_store_path_handle_before_fspath_hook(
    monkeypatch, tmp_path: Path,
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)
    attacker_called = False

    class HostilePathLike:
        def __fspath__(self):
            nonlocal attacker_called
            attacker_called = True
            raise AssertionError("store path-like hook must never execute")

    object.__setattr__(store, "workspace", HostilePathLike())

    with _active_runtime_profile(tmp_path / "workspace") as runtime_profile:
        with pytest.raises(
            BetfairStandardLimitPriceBoundError,
            match="workspace handle is not canonical",
        ):
            verify_product_betfair_standard_limit_price_bound(
                evidence=evidence,
                ledger=ledger,
                issuance_store=store,
                runtime_profile=runtime_profile,
                execution_plan_id=bound.execution_plan.plan_id,
                action_id=action.action_id,
            )

    assert attacker_called is False


def test_product_verifier_rejects_noncanonical_ledger_path_handle_before_fspath_hook(
    monkeypatch, tmp_path: Path,
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)
    attacker_called = False

    class HostilePathLike:
        def __fspath__(self):
            nonlocal attacker_called
            attacker_called = True
            raise AssertionError("ledger path-like hook must never execute")

    object.__setattr__(ledger, "path", HostilePathLike())

    with _active_runtime_profile(store.workspace) as runtime_profile:
        with pytest.raises(
            BetfairStandardLimitPriceBoundError,
            match="path handle is not canonical",
        ):
            verify_product_betfair_standard_limit_price_bound(
                evidence=evidence,
                ledger=ledger,
                issuance_store=store,
                runtime_profile=runtime_profile,
                execution_plan_id=bound.execution_plan.plan_id,
                action_id=action.action_id,
            )

    assert attacker_called is False

def test_product_verifier_rejects_runtime_workspace_descriptor_rebinding_before_getter(
    monkeypatch, tmp_path: Path,
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)
    attacker_called = False

    def attacker_workspace(_self):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("runtime workspace descriptor must never execute")

    with _active_runtime_profile(store.workspace) as runtime_profile:
        monkeypatch.setattr(type(runtime_profile), "workspace", property(attacker_workspace))
        with pytest.raises(
            BetfairStandardLimitPriceBoundError,
            match="product verifier authority changed",
        ):
            verify_product_betfair_standard_limit_price_bound(
                evidence=evidence,
                ledger=ledger,
                issuance_store=store,
                runtime_profile=runtime_profile,
                execution_plan_id=bound.execution_plan.plan_id,
                action_id=action.action_id,
            )

    assert attacker_called is False

def test_product_verifier_rejects_reopened_store_directory_redirection(
    monkeypatch, tmp_path: Path,
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)

    with _active_runtime_profile(store.workspace) as runtime_profile:
        monkeypatch.setattr(issuance_module, "_DIRECTORY", "attacker-issuance")
        with pytest.raises(
            BetfairStandardLimitPriceBoundError,
            match="reopen state changed",
        ):
            verify_product_betfair_standard_limit_price_bound(
                evidence=evidence,
                ledger=ledger,
                issuance_store=store,
                runtime_profile=runtime_profile,
                execution_plan_id=bound.execution_plan.plan_id,
                action_id=action.action_id,
            )



def test_product_verifier_rejects_path_resolve_rebinding_before_execution(
    monkeypatch, tmp_path: Path,
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)
    path_type = type(Path("."))
    attacker_called = False

    def attacker_resolve(self, *args, **kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound Path.resolve must never execute")

    with _active_runtime_profile(store.workspace) as runtime_profile:
        monkeypatch.setattr(path_type, "resolve", attacker_resolve)
        with pytest.raises(
            BetfairStandardLimitPriceBoundError,
            match="product verifier reopen authority changed",
        ):
            verify_product_betfair_standard_limit_price_bound(
                evidence=evidence,
                ledger=ledger,
                issuance_store=store,
                runtime_profile=runtime_profile,
                execution_plan_id=bound.execution_plan.plan_id,
                action_id=action.action_id,
            )

    assert attacker_called is False


def test_product_verifier_rejects_in_place_path_resolve_code_mutation_before_execution(
    monkeypatch, tmp_path: Path,
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)
    resolver = type(Path(".")).resolve
    original_code = resolver.__code__
    attacker_called = False

    def attacker_resolve(self, *args, **kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("mutated Path.resolve must never execute")

    try:
        resolver.__code__ = attacker_resolve.__code__
        with _active_runtime_profile(store.workspace) as runtime_profile:
            with pytest.raises(
                BetfairStandardLimitPriceBoundError,
                match="product verifier reopen authority changed",
            ):
                verify_product_betfair_standard_limit_price_bound(
                    evidence=evidence,
                    ledger=ledger,
                    issuance_store=store,
                    runtime_profile=runtime_profile,
                    execution_plan_id=bound.execution_plan.plan_id,
                    action_id=action.action_id,
                )
    finally:
        resolver.__code__ = original_code

    assert attacker_called is False
