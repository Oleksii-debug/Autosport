from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from autosport.forward_evaluation_universe_binding import (
    ForwardEvaluationUniverseBindingError,
    authorize_forward_source_receipts,
    resolve_forward_universe_members,
)
from autosport.provider_evaluation_universe import ProviderEvaluationUniverseStore


def _parent_helpers() -> dict[str, object]:
    return runpy.run_path(
        str(Path(__file__).with_name("test_forward_evaluation_universe_binding.py"))
    )


def test_exact_store_instance_load_rebinding_cannot_replace_durable_authority(
    tmp_path,
    monkeypatch,
) -> None:
    helpers = _parent_helpers()
    stored_universe = helpers["_stored_universe"]
    protocol_factory = helpers["_protocol"]
    opportunity_factory = helpers["_opportunities"]

    trusted_store = stored_universe(tmp_path, monkeypatch, empty=False)
    protocol = protocol_factory()
    expectations = resolve_forward_universe_members(
        store=trusted_store,
        protocol=protocol,
    )
    opportunities = opportunity_factory(protocol, expectations)

    trusted_ledger = ProviderEvaluationUniverseStore.load(trusted_store)
    assert trusted_ledger is not None

    empty_store = ProviderEvaluationUniverseStore(
        tmp_path / "empty-workspace",
        authority_id=trusted_store.authority_id,
        source_id=trusted_store.source_id,
        authority_root=tmp_path / "empty-authority",
    )

    # Remove the real durable universe behind the exact object. A canonical
    # unbound read now proves that this store has no forward-universe authority.
    trusted_store._store = empty_store._store
    assert ProviderEvaluationUniverseStore.load(trusted_store) is None

    # Python's exact-type check still succeeds after an instance method rebind.
    # A positive authority boundary must use the canonical read path, not this
    # caller-controlled virtual dispatch.
    trusted_store.load = lambda: trusted_ledger
    assert type(trusted_store) is ProviderEvaluationUniverseStore

    with pytest.raises(ForwardEvaluationUniverseBindingError):
        authorize_forward_source_receipts(
            store=trusted_store,
            protocol=protocol,
            opportunities=opportunities,
        )
