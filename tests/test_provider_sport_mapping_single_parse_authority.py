from __future__ import annotations

import sys
from pathlib import Path

import autosport.provider_sport_mapping as mapping


def test_curated_registration_invokes_canonical_evidence_parser_exactly_once(
    tmp_path: Path,
) -> None:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )
    parser_descriptor = vars(mapping.ProviderSportEvidence).get("from_exact_bytes")
    assert type(parser_descriptor) is classmethod
    parser_code = parser_descriptor.__func__.__code__
    parser_calls = 0
    previous_profile = sys.getprofile()

    def profile(frame, event, arg):
        nonlocal parser_calls
        del arg
        if event == "call" and frame.f_code is parser_code:
            parser_calls += 1
        return profile

    sys.setprofile(profile)
    try:
        binding = registry.register_curated(
            provider_namespace="betfair",
            provider_sport_id="1",
        )
    finally:
        sys.setprofile(previous_profile)

    assert parser_calls == 1
    assert binding.provider_namespace == "betfair"
    assert binding.provider_sport_id == "1"
    assert binding.canonical_sport == "football"

    reopened = mapping.ProviderSportMappingRegistry(registry.path)
    assert reopened.bindings == (binding,)
