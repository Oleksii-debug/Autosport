from __future__ import annotations

from datetime import UTC, datetime

import pytest

from autosport._forward_universe_backing_guard import (
    ForwardUniverseBackingGuardError,
    load_guarded_provider_evaluation_universe,
    resolve_forward_universe_backing_locator,
)
from autosport.forward_evaluation_universe_binding import (
    FORWARD_UNIVERSE_RULE_ID,
    FORWARD_UNIVERSE_RULE_SHA256,
    ForwardEvaluationUniverseBindingError,
    resolve_forward_universe_members,
)
from autosport.forward_evidence_completeness import ForwardEvidenceProtocolEnvelope
from autosport.provider_evaluation_universe import ProviderEvaluationUniverseStore


def _store(tmp_path, name: str) -> ProviderEvaluationUniverseStore:
    return ProviderEvaluationUniverseStore(
        tmp_path / name / "workspace",
        authority_id="provider-intake-1",
        source_id="parlayapi:table_tennis",
        authority_root=tmp_path / name / "authority",
    )


def _protocol() -> ForwardEvidenceProtocolEnvelope:
    return ForwardEvidenceProtocolEnvelope(
        campaign_id="campaign-1",
        scientific_protocol_sha256="1" * 64,
        candidate_universe_rule_id=FORWARD_UNIVERSE_RULE_ID,
        candidate_universe_rule_sha256=FORWARD_UNIVERSE_RULE_SHA256,
        forward_evaluation_policy_sha256="2" * 64,
        runtime_identity_sha256="3" * 64,
        baseline_set_sha256="4" * 64,
        protective_metric_set_sha256="5" * 64,
        cost_policy_sha256="6" * 64,
        precommit_anchor_lower=datetime(2026, 9, 20, 7, 0, tzinfo=UTC),
        precommit_anchor_upper=datetime(2026, 9, 20, 7, 30, tzinfo=UTC),
    )


def test_backing_locator_is_restart_stable_for_same_durable_workspace(tmp_path):
    workspace = tmp_path / "product" / "workspace"
    authority_root = tmp_path / "product" / "authority"
    first = ProviderEvaluationUniverseStore(
        workspace,
        authority_id="provider-intake-1",
        source_id="parlayapi:table_tennis",
        authority_root=authority_root,
    )
    first_locator = resolve_forward_universe_backing_locator(first)

    reopened = ProviderEvaluationUniverseStore(
        workspace,
        authority_id="provider-intake-1",
        source_id="parlayapi:table_tennis",
        authority_root=authority_root,
    )
    reopened_locator = resolve_forward_universe_backing_locator(reopened)

    assert reopened_locator == first_locator
    assert reopened_locator.locator_sha256 == first_locator.locator_sha256


def test_same_labels_on_different_durable_store_have_different_locator(tmp_path):
    first = _store(tmp_path, "first")
    second = _store(tmp_path, "second")

    first_locator = resolve_forward_universe_backing_locator(first)
    second_locator = resolve_forward_universe_backing_locator(second)

    assert first.authority_id == second.authority_id
    assert first.source_id == second.source_id
    assert first_locator.locator_sha256 != second_locator.locator_sha256


def test_coherent_private_backing_substitution_fails_closed(tmp_path):
    canonical = _store(tmp_path, "canonical")
    alternate = _store(tmp_path, "alternate")
    canonical_locator = resolve_forward_universe_backing_locator(canonical)
    alternate_locator = resolve_forward_universe_backing_locator(alternate)
    assert canonical_locator.locator_sha256 != alternate_locator.locator_sha256

    canonical._store = alternate._store
    canonical._intake = alternate._intake

    with pytest.raises(
        ForwardUniverseBackingGuardError,
        match="backing locator changed",
    ):
        resolve_forward_universe_backing_locator(canonical)


def test_public_forward_resolver_rejects_coherent_backing_substitution(tmp_path):
    canonical = _store(tmp_path, "canonical-public")
    alternate = _store(tmp_path, "alternate-public")
    resolve_forward_universe_backing_locator(canonical)

    canonical._store = alternate._store
    canonical._intake = alternate._intake

    with pytest.raises(
        ForwardEvaluationUniverseBindingError,
        match="backing locator authority changed",
    ):
        resolve_forward_universe_members(
            store=canonical,
            protocol=_protocol(),
        )


def test_guarded_load_ignores_outer_instance_load_rebinding(tmp_path):
    store = _store(tmp_path, "instance-load-rebind")
    expected_locator = resolve_forward_universe_backing_locator(store)
    store.load = lambda: object()

    ledger, locator = load_guarded_provider_evaluation_universe(store)

    assert ledger is None
    assert locator == expected_locator


def test_guarded_load_rechecks_locator_after_inner_read(tmp_path):
    canonical = _store(tmp_path, "during-read-canonical")
    alternate = _store(tmp_path, "during-read-alternate")
    resolve_forward_universe_backing_locator(canonical)
    backing = canonical._store
    original_read = backing._read_unlocked

    def swap_during_read():
        canonical._store = alternate._store
        canonical._intake = alternate._intake
        return original_read()

    backing._read_unlocked = swap_during_read

    with pytest.raises(
        ForwardUniverseBackingGuardError,
        match="changed during durable load",
    ):
        load_guarded_provider_evaluation_universe(canonical)
