from __future__ import annotations

from datetime import UTC, datetime

import pytest

import autosport.forward_evaluation_universe_binding as binding
from autosport.forward_evaluation_universe_binding import (
    FORWARD_UNIVERSE_RULE_ID,
    FORWARD_UNIVERSE_RULE_SHA256,
    ForwardEvaluationUniverseBindingError,
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


def test_module_global_load_alias_cannot_retarget_positive_resolution(
    tmp_path,
    monkeypatch,
):
    store = _empty_store(tmp_path)

    def attacker_controlled_load(_store):
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
        )


def test_captured_load_executable_mutation_fails_closed(tmp_path, monkeypatch):
    store = _empty_store(tmp_path)

    def attacker_controlled_load(self):
        return None

    monkeypatch.setattr(
        ProviderEvaluationUniverseStore.load,
        "__code__",
        attacker_controlled_load.__code__,
    )

    with pytest.raises(
        ForwardEvaluationUniverseBindingError,
        match="load authority changed",
    ):
        binding.resolve_forward_universe_members(
            store=store,
            protocol=_protocol(),
        )
