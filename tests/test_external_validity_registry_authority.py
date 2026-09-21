from __future__ import annotations

import pytest

from autosport.external_validity_registry import (
    ExternalValidityRegistryError,
    build_registered_external_validity_report,
)
from autosport.scientific_registry import ScientificRegistry


def test_wrapper_rejects_spoofed_registry_subclass_before_fake_get() -> None:
    calls: list[tuple[object, ...]] = []

    class SpoofedScientificRegistry(ScientificRegistry):
        def get(self, *args: object) -> object:
            calls.append(args)
            return object()

    spoofed = object.__new__(SpoofedScientificRegistry)

    with pytest.raises(
        ExternalValidityRegistryError,
        match="exact ScientificRegistry authority",
    ):
        build_registered_external_validity_report(
            spoofed,
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            (),
            candidate_evaluation_bundle_id="bundle-id:spoofed",
            baseline_evaluation_bundle_ids={},
        )

    assert calls == []
