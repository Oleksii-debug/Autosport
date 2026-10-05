from __future__ import annotations

from pathlib import Path

import pytest

import autosport.real_execution_ledger as ledger_module
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

    original_resolve = path_type.resolve
    with _active_runtime_profile(store.workspace) as runtime_profile:
        try:
            path_type.resolve = attacker_resolve
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
            path_type.resolve = original_resolve

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

    with _active_runtime_profile(store.workspace) as runtime_profile:
        try:
            resolver.__code__ = attacker_resolve.__code__
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

def test_product_verifier_rejects_rebound_issuance_path_factory_before_execution(
    monkeypatch, tmp_path: Path,
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)
    attacker_called = False

    def attacker_path(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound issuance Path must never execute")

    with _active_runtime_profile(store.workspace) as runtime_profile:
        monkeypatch.setattr(issuance_module, "Path", attacker_path)
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


def test_product_verifier_rejects_rebound_ledger_path_factory_before_execution(
    monkeypatch, tmp_path: Path,
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)
    attacker_called = False

    def attacker_path(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound ledger Path must never execute")

    with _active_runtime_profile(store.workspace) as runtime_profile:
        monkeypatch.setattr(ledger_module, "Path", attacker_path)
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


def test_product_verifier_rejects_rebound_ledger_rlock_factory_before_execution(
    monkeypatch, tmp_path: Path,
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)
    attacker_called = False

    def attacker_rlock(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound ledger RLock must never execute")

    with _active_runtime_profile(store.workspace) as runtime_profile:
        monkeypatch.setattr(ledger_module.threading, "RLock", attacker_rlock)
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


def test_product_verifier_rejects_rebound_path_mkdir_before_execution(
    monkeypatch, tmp_path: Path,
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)
    path_type = type(Path("."))
    original_mkdir = path_type.mkdir
    attacker_called = False

    def attacker_mkdir(self, *args, **kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound Path.mkdir must never execute")

    with _active_runtime_profile(store.workspace) as runtime_profile:
        try:
            path_type.mkdir = attacker_mkdir
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
            path_type.mkdir = original_mkdir

    assert attacker_called is False


def test_product_verifier_rejects_rebound_path_with_name_before_execution(
    monkeypatch, tmp_path: Path,
) -> None:
    bound, action, store, ledger, evidence = _case(monkeypatch, tmp_path)
    path_type = type(Path("."))
    original_with_name = path_type.with_name
    attacker_called = False

    def attacker_with_name(self, *args, **kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound Path.with_name must never execute")

    with _active_runtime_profile(store.workspace) as runtime_profile:
        try:
            path_type.with_name = attacker_with_name
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
            path_type.with_name = original_with_name

    assert attacker_called is False

