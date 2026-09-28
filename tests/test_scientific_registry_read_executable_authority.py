from __future__ import annotations

import pytest

from autosport._scientific_registry_read_authority import (
    ScientificRegistryReadAuthorityError,
    require_scientific_registry_read_authority,
)
from autosport.scientific_registry import ScientificRegistry


def test_runtime_get_rebind_is_rejected_before_attacker_executes(monkeypatch):
    calls: list[str] = []

    def forged_get(self, record_type, record_id):
        calls.append(f"{record_type}:{record_id}")
        return None

    monkeypatch.setattr(ScientificRegistry, "get", forged_get)

    with pytest.raises(
        ScientificRegistryReadAuthorityError,
        match="ScientificRegistry.*(authority|semantics|changed)",
    ):
        require_scientific_registry_read_authority()

    assert calls == []


def test_runtime_causal_precedes_rebind_is_rejected_before_attacker_executes(monkeypatch):
    calls: list[str] = []

    def forged_causal_precedes(self, before_type, before_id, after_type, after_id):
        calls.append(f"{before_type}:{before_id}->{after_type}:{after_id}")
        return True

    monkeypatch.setattr(ScientificRegistry, "causal_precedes", forged_causal_precedes)

    with pytest.raises(
        ScientificRegistryReadAuthorityError,
        match="ScientificRegistry.*(authority|semantics|changed)",
    ):
        require_scientific_registry_read_authority()

    assert calls == []


@pytest.mark.parametrize(
    "dependency_name",
    (
        "_promotion_effective_sample_bundle_from_entry",
        "_entry",
    ),
)
def test_runtime_promotion_ess_transitive_helper_rebind_is_rejected_before_use(
    tmp_path,
    monkeypatch,
    dependency_name,
):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / f"scientific-registry-{dependency_name}.json"
    )
    calls: list[str] = []

    def forged(*args, **kwargs):
        calls.append(dependency_name)
        return None

    monkeypatch.setattr(ScientificRegistry, dependency_name, staticmethod(forged))

    with pytest.raises(
        ScientificRegistryReadAuthorityError,
        match=dependency_name,
    ):
        ScientificRegistry(registry.path)

    assert calls == []
