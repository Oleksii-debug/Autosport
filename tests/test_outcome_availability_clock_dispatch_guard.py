from __future__ import annotations

from pathlib import Path
from types import FunctionType

import pytest

import autosport._outcome_availability_registry_serialization as availability
from autosport.outcome_trust import OutcomeLineageTrustError
from autosport.run_registry import RunRegistry


def _reachable_functions(root: FunctionType) -> tuple[FunctionType, ...]:
    pending: list[object] = [root]
    seen: set[int] = set()
    found: list[FunctionType] = []
    while pending:
        value = pending.pop()
        if not isinstance(value, FunctionType) or id(value) in seen:
            continue
        seen.add(id(value))
        found.append(value)
        if value.__defaults__:
            pending.extend(value.__defaults__)
        if value.__kwdefaults__:
            pending.extend(value.__kwdefaults__.values())
        wrapped = getattr(value, "__wrapped__", None)
        if wrapped is not None:
            pending.append(wrapped)
        if value.__closure__:
            for cell in value.__closure__:
                try:
                    pending.append(cell.cell_contents)
                except ValueError:
                    pass
    return tuple(found)


def _reachable_named(root: FunctionType, name: str) -> FunctionType:
    matches = [
        function
        for function in _reachable_functions(root)
        if function is not root and function.__name__ == name
    ]
    assert len(matches) == 1, [function.__name__ for function in matches]
    return matches[0]


def _new_registry(tmp_path: Path) -> RunRegistry:
    return RunRegistry.initialize_pristine(tmp_path / "run-registry.json")


def _attempt_begin(registry: RunRegistry) -> None:
    registry.begin(
        "a" * 64,
        "b" * 64,
        "strategy",
        "run-metadata-falsifier",
    )


def test_sealed_product_clock_exposes_no_mutable_default_authority() -> None:
    sampler = availability._sealed_product_utc_now
    assert isinstance(sampler, FunctionType)
    assert sampler.__defaults__ is None
    assert getattr(RunRegistry.begin, "_autosport_clock_metadata_sealed", False)


def test_sampler_module_dispatch_rebind_fails_closed_before_begin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = _new_registry(tmp_path)
    original = availability._sealed_product_utc_now
    monkeypatch.setattr(
        availability,
        "_sealed_product_utc_now",
        lambda: "1900-01-01T00:00:00.000000Z",
    )

    with pytest.raises(
        OutcomeLineageTrustError,
        match="outcome availability clock sampler dispatch was rebound",
    ):
        _attempt_begin(registry)

    monkeypatch.setattr(availability, "_sealed_product_utc_now", original)


def test_hidden_product_clock_globals_mutation_fails_closed_before_begin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = _new_registry(tmp_path)
    sampler = availability._sealed_product_utc_now
    clock_clone = _reachable_named(sampler, "_utc_now")
    monkeypatch.setitem(clock_clone.__globals__, "datetime", object())

    with pytest.raises(
        OutcomeLineageTrustError,
        match=r"frozen product UTC clock global 'datetime' was rebound",
    ):
        _attempt_begin(registry)


def test_hidden_begin_sampler_global_mutation_fails_closed_before_begin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = _new_registry(tmp_path)
    begin_clone = _reachable_named(
        RunRegistry.begin,
        "_begin_with_causal_outcome_publication",
    )
    monkeypatch.setitem(
        begin_clone.__globals__,
        "_sealed_product_utc_now",
        lambda: "1900-01-01T00:00:00.000000Z",
    )

    with pytest.raises(
        OutcomeLineageTrustError,
        match=r"frozen causal RunRegistry begin global '_sealed_product_utc_now' was rebound",
    ):
        _attempt_begin(registry)
