from __future__ import annotations

import pytest

from autosport._forward_universe_backing_guard import (
    ForwardUniverseBackingGuardError,
    resolve_forward_universe_backing_locator,
)
from autosport.provider_evaluation_universe import ProviderEvaluationUniverseStore


def _store(tmp_path, name: str) -> ProviderEvaluationUniverseStore:
    return ProviderEvaluationUniverseStore(
        tmp_path / name / "workspace",
        authority_id="provider-intake-1",
        source_id="parlayapi:table_tennis",
        authority_root=tmp_path / name / "authority",
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
