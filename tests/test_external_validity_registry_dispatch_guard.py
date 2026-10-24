from __future__ import annotations

import pytest

from autosport import _external_validity_registry_core as registry_core
from autosport import external_validity_registry as registry_module
from autosport.external_validity_registry import ExternalValidityRegistryError
from autosport.scientific_registry import ScientificRegistry

from test_external_validity_registry import (
    _evaluations,
    _protocol,
    _seed_happy_registry,
)


def _call_happy_inputs(tmp_path):
    protocol = _protocol()
    candidate, baselines = _evaluations(protocol)
    registry, candidate_bundle_id, baseline_ids = _seed_happy_registry(
        tmp_path,
        protocol,
        candidate,
        baselines,
    )
    return (
        registry,
        protocol,
        candidate,
        baselines,
        candidate_bundle_id,
        baseline_ids,
    )


def test_public_build_is_dispatch_guarded() -> None:
    assert getattr(
        registry_module.build_registered_external_validity_report,
        "_autosport_external_validity_registry_dispatch_guard",
        False,
    )


def test_public_guard_does_not_retain_predecessor_build_callable_in_closure() -> None:
    guarded = registry_module.build_registered_external_validity_report
    retained = []
    for cell in guarded.__closure__ or ():
        value = cell.cell_contents
        if (
            callable(value)
            and getattr(value, "__name__", None)
            == "build_registered_external_validity_report"
            and value is not guarded
        ):
            retained.append(value)
    assert retained == []


def test_self_confirming_cached_get_and_class_get_rebind_fails_before_forged_read(
    tmp_path,
    monkeypatch,
) -> None:
    (
        registry,
        protocol,
        candidate,
        baselines,
        candidate_bundle_id,
        baseline_ids,
    ) = _call_happy_inputs(tmp_path)
    calls: list[tuple[object, ...]] = []

    def forged_get(_self, *args: object) -> object:
        calls.append(args)
        return object()

    # Predecessor bypass: the mutable cached expected delegate and the live class
    # method could be rebound to the same forged object, making the old identity
    # check self-confirming. The guarded public path must now fail before forged_get.
    monkeypatch.setattr(
        registry_core,
        "_SCIENTIFIC_REGISTRY_GET",
        forged_get,
    )
    monkeypatch.setattr(ScientificRegistry, "get", forged_get)

    with pytest.raises(
        ExternalValidityRegistryError,
        match="executable read authority was rebound",
    ):
        registry_module.build_registered_external_validity_report(
            registry,
            protocol,
            candidate,
            baselines,
            candidate_evaluation_bundle_id=candidate_bundle_id,
            baseline_evaluation_bundle_ids=baseline_ids,
        )

    assert calls == []


def test_core_registry_get_rebind_is_rejected_before_forged_read(
    tmp_path,
    monkeypatch,
) -> None:
    (
        registry,
        protocol,
        candidate,
        baselines,
        candidate_bundle_id,
        baseline_ids,
    ) = _call_happy_inputs(tmp_path)
    calls: list[tuple[object, ...]] = []

    def forged_registry_get(*args: object, **kwargs: object) -> object:
        calls.append((*args, kwargs))
        return object()

    monkeypatch.setattr(registry_core, "_registry_get", forged_registry_get)

    with pytest.raises(
        ExternalValidityRegistryError,
        match="adapter dispatch changed",
    ):
        registry_module.build_registered_external_validity_report(
            registry,
            protocol,
            candidate,
            baselines,
            candidate_evaluation_bundle_id=candidate_bundle_id,
            baseline_evaluation_bundle_ids=baseline_ids,
        )

    assert calls == []
