from __future__ import annotations

from pathlib import Path

import pytest

import autosport._product_runtime_continuous_session_entry_guard as runtime_entry_guard
import autosport.continuous_session_entry as session_entry
from autosport.continuous_session import ContinuousSessionCoordinator, ContinuousSessionError
from autosport.event_lifecycle import CatalogPage
from autosport.product_runtime import build_autonomous_product_runtime


class _Source:
    source_id = "provider-a"
    stream_epoch = "epoch-1"

    def fetch_catalog_page(self, checkpoint):
        del checkpoint
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor="catalog-1",
            position=1,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        del checkpoint, records, max_items
        return ()

    def resolve_event(self, delta):
        raise AssertionError(f"unexpected delta resolution: {delta!r}")


def _runtime(tmp_path: Path):
    return build_autonomous_product_runtime(
        workspace=tmp_path,
        source=_Source(),
        clock=lambda: "2026-09-27T05:20:00+00:00",
        sleep=lambda _: None,
        initial_bankroll="100",
    )


def test_product_runtime_tick_uses_nonvirtual_session_entry(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    coordinator_type = ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator_type, "__dict__")
    original_getattribute = coordinator_dict["__getattribute__"]
    hostile_calls = 0

    def hostile_tick(self):
        nonlocal hostile_calls
        del self
        hostile_calls += 1
        return object()

    def hostile_getattribute(self, name):
        if name == "tick":
            return hostile_tick.__get__(self, coordinator_type)
        return object.__getattribute__(self, name)

    try:
        # Coherence/status reads still work, but mutable coordinator lookup would
        # return this hostile tick before the original method could run any guard.
        # The canonical product loop therefore has to cross tick_continuous_session
        # non-virtually and reject the changed lookup root before hostile execution.
        type.__setattr__(coordinator_type, "__getattribute__", hostile_getattribute)
        with pytest.raises(ContinuousSessionError):
            runtime.tick()
        assert hostile_calls == 0
    finally:
        type.__setattr__(coordinator_type, "__getattribute__", original_getattribute)
        runtime.close()


def test_runtime_tick_does_not_rediscover_mutable_guard_module_entry_alias(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    marker = "_tick_continuous_session"
    had_alias = hasattr(runtime_entry_guard, marker)
    original_alias = getattr(runtime_entry_guard, marker, None)
    hostile_calls = 0

    def hostile_entry(coordinator):
        nonlocal hostile_calls
        del coordinator
        hostile_calls += 1
        return object()

    try:
        # The predecessor product guard called a module-global alias by name. Rebinding
        # that alias redirected PAPER execution without touching the sealed public
        # session entry. The installed runtime tick must own its exact entry by closure.
        setattr(runtime_entry_guard, marker, hostile_entry)
        result = runtime.tick()
        assert result.cycle_index == 1
        assert hostile_calls == 0
    finally:
        if had_alias:
            setattr(runtime_entry_guard, marker, original_alias)
        else:
            delattr(runtime_entry_guard, marker)
        runtime.close()


def test_runtime_tick_fails_closed_if_public_session_entry_binding_moves(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    canonical_entry = session_entry.tick_continuous_session
    hostile_calls = 0

    def hostile_entry(coordinator):
        nonlocal hostile_calls
        del coordinator
        hostile_calls += 1
        return object()

    try:
        session_entry.tick_continuous_session = hostile_entry
        with pytest.raises(ContinuousSessionError, match="entry authority changed"):
            runtime.tick()
        assert hostile_calls == 0
    finally:
        session_entry.tick_continuous_session = canonical_entry
        runtime.close()
