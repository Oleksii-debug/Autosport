from __future__ import annotations

from pathlib import Path
from types import FunctionType

import pytest

import autosport._strategy_model_factory_publish_receipt_guard as receipt_guard
import autosport.strategy_model_factory as factory_module
from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore


def _closure_cell(function: FunctionType, name: str):
    names = function.__code__.co_freevars
    closure = function.__closure__
    assert closure is not None
    assert name in names
    return closure[names.index(name)]


def test_sealed_issuer_rejects_verifier_closure_retarget_before_hostile_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installed dispatch seal must not trust a caller-writable verifier cell."""

    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    installed_issuer = factory_module._record_committed_factory_publish
    assert type(installed_issuer) is FunctionType

    sealed_record_cell = _closure_cell(installed_issuer, "sealed_record")
    sealed_record = sealed_record_cell.cell_contents
    assert type(sealed_record) is FunctionType

    require_cell = _closure_cell(sealed_record, "require")
    original_require = require_cell.cell_contents
    hostile_calls = 0

    def no_op_require(*_args: object, **_kwargs: object) -> None:
        return None

    def hostile_reader(_path: object) -> object:
        nonlocal hostile_calls
        hostile_calls += 1
        raise AssertionError("hostile transaction reader executed")

    # Mutate only the installed seal's verifier capability and the already-covered
    # underlying dispatch target.  A sound seal must retain an independent authority
    # root and reject before the hostile reader is reached.
    require_cell.cell_contents = no_op_require
    monkeypatch.setattr(receipt_guard, "_READ_PUBLISH_TRANSACTION", hostile_reader)
    try:
        with pytest.raises(RuntimeError):
            installed_issuer(registry, store)
    finally:
        require_cell.cell_contents = original_require

    assert hostile_calls == 0
    assert not store._publish_commit_ledger_path().exists()
