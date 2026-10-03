from __future__ import annotations

from pathlib import Path

import pytest

import autosport.provider_sport_mapping as mapping


class _HostileText(str):
    def __new__(cls, value: str, calls: list[str]):
        instance = super().__new__(cls, value)
        instance.calls = calls
        return instance

    def __hash__(self) -> int:
        self.calls.append("hash")
        raise AssertionError("caller-defined selector hash executed")

    def __eq__(self, other: object) -> bool:
        self.calls.append("eq")
        raise AssertionError("caller-defined selector equality executed")


def test_curated_selector_rejects_non_exact_strings_before_caller_callbacks(
    tmp_path: Path,
) -> None:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )
    calls: list[str] = []
    hostile_namespace = _HostileText("betfair", calls)

    with pytest.raises(mapping.ProviderSportMappingError, match="curation authority"):
        registry.register_curated(
            provider_namespace=hostile_namespace,  # type: ignore[arg-type]
            provider_sport_id="1",
        )

    assert calls == []
    assert registry.bindings == ()


def test_curated_selector_rejects_hostile_id_before_caller_callbacks(
    tmp_path: Path,
) -> None:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )
    calls: list[str] = []
    hostile_id = _HostileText("1", calls)

    with pytest.raises(mapping.ProviderSportMappingError, match="curation authority"):
        registry.register_curated(
            provider_namespace="betfair",
            provider_sport_id=hostile_id,  # type: ignore[arg-type]
        )

    assert calls == []
    assert registry.bindings == ()
