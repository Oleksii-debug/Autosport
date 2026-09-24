from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from autosport.registered_strategy_model_runtime import (
    RegisteredStrategyModelRuntimeError,
    resolve_registered_strategy_model,
)


def _fixture_module() -> ModuleType:
    fixture_path = Path(__file__).with_name("test_registered_strategy_model_runtime.py")
    spec = importlib.util.spec_from_file_location(
        "_registered_strategy_model_runtime_fixture",
        fixture_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load registered runtime fixture module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _foreign_question_entry(fixture: ModuleType) -> dict[str, object]:
    return fixture._entry(
        "ResearchQuestion",
        "foreign-question-v1",
        "2025-12-31T23:59:59Z",
        {
            "question_id": "foreign-question-v1",
            "statement": "independent valid registry history",
            "source_sha256": fixture.A,
            "created_at": "2025-12-31T23:59:59Z",
        },
    )


def _write_registry(path: Path, state: dict[str, object]) -> None:
    path.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def test_rejects_valid_receipt_copied_onto_divergent_registry_prefix(
    tmp_path: Path,
) -> None:
    fixture = _fixture_module()
    fixture._write_workspace(tmp_path)
    registry_path = tmp_path / "scientific_registry.json"
    state = json.loads(registry_path.read_text(encoding="utf-8"))
    state["records"].insert(0, _foreign_question_entry(fixture))
    _write_registry(registry_path, state)

    with pytest.raises(
        RegisteredStrategyModelRuntimeError,
        match="final registry is not a current registry prefix",
    ):
        resolve_registered_strategy_model(
            tmp_path.resolve(),
            strategy_version_id="strategy-v1",
            as_of=fixture.RUNTIME_AFTER_PUBLICATION,
        )


def test_allows_legitimate_registry_appends_after_factory_publication(
    tmp_path: Path,
) -> None:
    fixture = _fixture_module()
    fixture._write_workspace(tmp_path)
    registry_path = tmp_path / "scientific_registry.json"
    state = json.loads(registry_path.read_text(encoding="utf-8"))
    state["records"].append(_foreign_question_entry(fixture))
    _write_registry(registry_path, state)

    runtime = resolve_registered_strategy_model(
        tmp_path.resolve(),
        strategy_version_id="strategy-v1",
        as_of=fixture.RUNTIME_AFTER_PUBLICATION,
    )

    assert runtime.model_version_id == "model-v1"
    assert runtime.strategy_version_id == "strategy-v1"
