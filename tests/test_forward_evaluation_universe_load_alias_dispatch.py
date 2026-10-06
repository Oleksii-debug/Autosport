from __future__ import annotations

from datetime import UTC, datetime

import pytest

import autosport.forward_evaluation_universe_binding as binding
from autosport.forward_evaluation_universe_binding import (
    FORWARD_UNIVERSE_RULE_ID,
    FORWARD_UNIVERSE_RULE_SHA256,
    ForwardEvaluationUniverseBindingError,
    ForwardUniversePrecommitLocator,
)
from autosport.forward_evidence_completeness import ForwardEvidenceProtocolEnvelope
from autosport.provider_evaluation_universe import ProviderEvaluationUniverseStore


def _protocol() -> ForwardEvidenceProtocolEnvelope:
    return ForwardEvidenceProtocolEnvelope(
        campaign_id="campaign-1",
        scientific_protocol_sha256="1" * 64,
        candidate_universe_rule_id=FORWARD_UNIVERSE_RULE_ID,
        candidate_universe_rule_sha256=FORWARD_UNIVERSE_RULE_SHA256,
        forward_evaluation_policy_sha256="7" * 64,
        runtime_identity_sha256="6" * 64,
        baseline_set_sha256="8" * 64,
        protective_metric_set_sha256="9" * 64,
        cost_policy_sha256="a" * 64,
        precommit_anchor_lower=datetime(2026, 9, 20, 7, 0, tzinfo=UTC),
        precommit_anchor_upper=datetime(2026, 9, 20, 7, 30, tzinfo=UTC),
    )


def _empty_store(tmp_path) -> ProviderEvaluationUniverseStore:
    return ProviderEvaluationUniverseStore(
        tmp_path / "workspace",
        authority_id="provider-intake-1",
        source_id="parlay:table_tennis",
        authority_root=tmp_path / "authority",
    )


def _precommit_locator(tmp_path) -> ForwardUniversePrecommitLocator:
    workspace = (tmp_path / "precommit-workspace").resolve()
    return ForwardUniversePrecommitLocator(
        manifest_path=workspace / "campaign-precommit.json",
        workspace=workspace,
    )


def test_module_global_load_alias_cannot_retarget_positive_resolution(
    tmp_path,
    monkeypatch,
):
    store = _empty_store(tmp_path)
    calls = 0

    def attacker_controlled_load(_store):
        nonlocal calls
        calls += 1
        raise AssertionError("mutable module-global load alias was dispatched")

    monkeypatch.setattr(
        binding,
        "_CANONICAL_PROVIDER_UNIVERSE_LOAD",
        attacker_controlled_load,
    )

    with pytest.raises(
        ForwardEvaluationUniverseBindingError,
        match="not durably available",
    ):
        binding.resolve_forward_universe_members(
            store=store,
            protocol=_protocol(),
            precommit=_precommit_locator(tmp_path),
        )
    assert calls == 0


def test_module_global_expectation_resolver_cannot_retarget_public_positive_api(
    tmp_path,
    monkeypatch,
):
    store = _empty_store(tmp_path)
    precommit = _precommit_locator(tmp_path)
    calls = 0

    def attacker_controlled_resolver(**_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("mutable module-global expectation resolver was dispatched")

    monkeypatch.setattr(
        binding,
        "_load_expectations",
        attacker_controlled_resolver,
        raising=False,
    )

    with pytest.raises(
        ForwardEvaluationUniverseBindingError,
        match="not durably available",
    ):
        binding.resolve_forward_universe_members(
            store=store,
            protocol=_protocol(),
            precommit=precommit,
        )
    with pytest.raises(
        ForwardEvaluationUniverseBindingError,
        match="not durably available",
    ):
        binding.authorize_forward_source_receipts(
            store=store,
            protocol=_protocol(),
            precommit=precommit,
            opportunities=(),
        )
    assert calls == 0


def _hostile_two_cell_callable():
    left = object()
    right = object()

    def hostile(**_kwargs):
        if left is not right:
            raise AssertionError("captured public resolver wrapper was dispatched")
        raise AssertionError("unreachable")

    return hostile


def test_captured_public_resolver_executable_mutation_fails_closed(
    tmp_path,
    monkeypatch,
):
    store = _empty_store(tmp_path)
    public = binding.resolve_forward_universe_members
    sealed = next(
        (
            cell.cell_contents
            for cell in (public.__closure__ or ())
            if getattr(cell.cell_contents, "__name__", None) == "_sealed_load_expectations"
        ),
        None,
    )
    assert sealed is not None
    hostile = _hostile_two_cell_callable()
    assert len(hostile.__code__.co_freevars) == len(sealed.__code__.co_freevars)
    monkeypatch.setattr(sealed, "__code__", hostile.__code__)

    with pytest.raises(
        ForwardEvaluationUniverseBindingError,
        match="public resolver dispatch changed",
    ):
        public(
            store=store,
            protocol=_protocol(),
            precommit=_precommit_locator(tmp_path),
        )

