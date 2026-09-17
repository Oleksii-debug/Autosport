from autosport.scientific_registry_views import strategy_state_projection


class _RegistryProbe:
    def __init__(self) -> None:
        self.champion_call: tuple[str, str | None] | None = None

    def causal_records(self, record_type: str, *, as_of: str):
        return ()

    def champion_strategy(
        self,
        *,
        as_of: str,
        canonical_strategy_id: str | None = None,
    ) -> None:
        self.champion_call = (as_of, canonical_strategy_id)
        return None


def test_strategy_state_projection_scopes_champion_lookup_to_context() -> None:
    registry = _RegistryProbe()
    as_of = "2026-01-05T00:00:00+00:00"
    canonical_strategy_id = "predictive-edge:soccer:match-winner"

    assert strategy_state_projection(
        registry,  # type: ignore[arg-type]
        canonical_strategy_id,
        as_of=as_of,
    ) == ()
    assert registry.champion_call == (as_of, canonical_strategy_id)
