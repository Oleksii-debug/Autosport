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


def _checked_boundary(root: FunctionType) -> object:
    assert root.__closure__ is not None
    candidates = []
    for cell in root.__closure__:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if not isinstance(value, FunctionType) and callable(value):
            candidates.append(value)
    assert len(candidates) == 1
    return candidates[0]


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
    assert callable(sampler)
    assert not isinstance(sampler, FunctionType)
    assert getattr(sampler, "__defaults__", None) is None
    assert getattr(RunRegistry.begin, "_autosport_clock_metadata_sealed", False)
    assert getattr(RunRegistry.begin, "_autosport_predecessor_unreachable", False)


def test_causal_begin_declares_same_process_reflection_resistance_unproven() -> None:
    checked_begin = _checked_boundary(RunRegistry.begin)
    raw_begin = object.__getattribute__(checked_begin, "_function")

    # Public metadata traversal is sealed, but arbitrary same-process private-slot
    # reflection can still recover a raw FunctionType capability. Keep that stronger
    # tamper-resistance claim mechanically false rather than overstating authority.
    assert isinstance(raw_begin, FunctionType)
    assert (
        getattr(
            RunRegistry.begin,
            "_autosport_same_process_reflection_tamper_resistance_proven",
            None,
        )
        is False
    )


def test_public_begin_metadata_does_not_expose_authority_predecessor() -> None:
    names = {function.__name__ for function in _reachable_functions(RunRegistry.begin)}
    assert "_begin_with_causal_outcome_publication" not in names
    assert "_utc_now" not in names


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


def test_checked_private_clock_global_mutation_fails_closed_before_begin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = _new_registry(tmp_path)
    sampler = availability._sealed_product_utc_now
    clock_clone = object.__getattribute__(sampler, "_function")
    assert isinstance(clock_clone, FunctionType)
    monkeypatch.setitem(clock_clone.__globals__, "datetime", object())

    # The raw clone is no longer reachable through public function metadata, but
    # even deliberate private-object introspection cannot make the public authority
    # accept its mutated dependency.
    with pytest.raises(
        OutcomeLineageTrustError,
        match=r"frozen product UTC clock global 'datetime' was rebound",
    ):
        sampler()


def test_checked_private_begin_global_mutation_fails_closed_before_begin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = _new_registry(tmp_path)
    checked_begin = _checked_boundary(RunRegistry.begin)
    begin_clone = object.__getattribute__(checked_begin, "_function")
    assert isinstance(begin_clone, FunctionType)
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
