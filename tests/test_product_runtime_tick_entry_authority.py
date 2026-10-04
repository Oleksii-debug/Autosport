from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from autosport.event_lifecycle import CatalogPage
from autosport.product_entrypoint import ProductRuntimeError, run_product
from autosport.product_runtime import (
    AutonomousProductRuntime,
    ProductCompositionError,
    build_autonomous_product_runtime,
)
from autosport.product_runtime_entry import tick_autonomous_product_runtime


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


def test_product_runtime_entry_tick_does_not_materialize_settlement_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=_Source(),
        clock=lambda: "2026-09-27T05:44:59+00:00",
        sleep=lambda _: None,
        initial_bankroll="100",
    )

    def forbidden_history_scan(*_args, **_kwargs):
        raise AssertionError(
            "canonical product tick materialized settlement history"
        )

    monkeypatch.setattr(
        runtime.coordinator._state,
        "_load_evidence_history",
        forbidden_history_scan,
    )
    try:
        result = tick_autonomous_product_runtime(runtime)
        assert result.session_id == runtime.coordinator.session_id
    finally:
        runtime.close()


def test_product_runtime_entry_rejects_direct_tick_slot_replacement(tmp_path: Path) -> None:
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=_Source(),
        clock=lambda: "2026-09-27T05:45:00+00:00",
        sleep=lambda _: None,
        initial_bankroll="100",
    )
    runtime_type = AutonomousProductRuntime
    runtime_dict = type.__getattribute__(runtime_type, "__dict__")
    original_tick = runtime_dict["tick"]
    hostile_calls = 0

    def hostile_tick(self):
        nonlocal hostile_calls
        del self
        hostile_calls += 1
        return object()

    try:
        type.__setattr__(runtime_type, "tick", hostile_tick)
        with pytest.raises(ProductCompositionError, match="tick dispatch changed"):
            tick_autonomous_product_runtime(runtime)
        assert hostile_calls == 0
    finally:
        type.__setattr__(runtime_type, "tick", original_tick)
        runtime.close()


def test_supported_product_loop_rejects_direct_runtime_tick_slot_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_module = types.ModuleType("autosport_test_wave_m_runtime_source")
    source_module.make_source = _Source
    monkeypatch.setitem(sys.modules, source_module.__name__, source_module)

    runtime_type = AutonomousProductRuntime
    runtime_dict = type.__getattribute__(runtime_type, "__dict__")
    original_tick = runtime_dict["tick"]
    hostile_calls = 0

    def hostile_tick(self):
        nonlocal hostile_calls
        del self
        hostile_calls += 1
        return object()

    try:
        type.__setattr__(runtime_type, "tick", hostile_tick)
        with pytest.raises(ProductRuntimeError) as raised:
            run_product(
                workspace=tmp_path / "product",
                source_factory=f"{source_module.__name__}:make_source",
                initial_bankroll="100",
                max_cycles=1,
                poll_seconds=0,
                sleep=lambda _: None,
                install_signal_handlers=False,
            )
        assert isinstance(raised.value.__cause__, ProductCompositionError)
        assert "tick dispatch changed" in str(raised.value.__cause__)
        assert hostile_calls == 0
    finally:
        type.__setattr__(runtime_type, "tick", original_tick)


def test_product_runtime_entry_rejects_tick_wrapper_code_mutation(tmp_path: Path) -> None:
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=_Source(),
        clock=lambda: "2026-09-27T05:45:01+00:00",
        sleep=lambda _: None,
        initial_bankroll="100",
    )
    runtime_type = AutonomousProductRuntime
    canonical_tick = type.__getattribute__(runtime_type, "__dict__")["tick"]
    original_code = canonical_tick.__code__
    hostile_calls = 0

    def hostile_wrapper(self):
        nonlocal hostile_calls
        del self
        hostile_calls += 1
        return object()

    try:
        canonical_tick.__code__ = hostile_wrapper.__code__
        with pytest.raises(ProductCompositionError, match="wrapper executable changed"):
            tick_autonomous_product_runtime(runtime)
        assert hostile_calls == 0
    finally:
        canonical_tick.__code__ = original_code
        runtime.close()


def test_product_runtime_entry_rejects_tick_wrapper_closure_target_mutation(
    tmp_path: Path,
) -> None:
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=_Source(),
        clock=lambda: "2026-09-27T05:45:02+00:00",
        sleep=lambda _: None,
        initial_bankroll="100",
    )
    runtime_type = AutonomousProductRuntime
    canonical_tick = type.__getattribute__(runtime_type, "__dict__")["tick"]
    closure = canonical_tick.__closure__
    assert closure is not None and len(closure) == 1
    target_cell = closure[0]
    original_target = target_cell.cell_contents
    assert original_target is canonical_tick.__wrapped__
    hostile_calls = 0

    def hostile_inner(self):
        nonlocal hostile_calls
        del self
        hostile_calls += 1
        return object()

    try:
        target_cell.cell_contents = hostile_inner
        assert canonical_tick.__wrapped__ is original_target
        with pytest.raises(ProductCompositionError, match="closure target changed"):
            tick_autonomous_product_runtime(runtime)
        assert hostile_calls == 0
    finally:
        target_cell.cell_contents = original_target
        runtime.close()


def test_product_runtime_entry_rejects_coordinated_inner_guard_closure_retargeting(
    tmp_path: Path,
) -> None:
    runtime = build_autonomous_product_runtime(
        workspace=tmp_path,
        source=_Source(),
        clock=lambda: "2026-09-27T05:45:03+00:00",
        sleep=lambda _: None,
        initial_bankroll="100",
    )
    runtime_type = AutonomousProductRuntime
    canonical_tick = type.__getattribute__(runtime_type, "__dict__")["tick"]
    canonical_inner = canonical_tick.__wrapped__
    inner_closure = canonical_inner.__closure__
    assert inner_closure is not None
    cells = dict(zip(canonical_inner.__code__.co_freevars, inner_closure, strict=True))
    assert {"canonical_entry", "canonical_entry_code", "entry_module"} <= set(cells)
    originals = {
        name: cells[name].cell_contents
        for name in ("canonical_entry", "canonical_entry_code", "entry_module")
    }
    hostile_calls = 0

    def hostile_entry(coordinator):
        nonlocal hostile_calls
        del coordinator
        hostile_calls += 1
        return object()

    fake_entry_module = types.SimpleNamespace(tick_continuous_session=hostile_entry)

    try:
        cells["canonical_entry"].cell_contents = hostile_entry
        cells["canonical_entry_code"].cell_contents = hostile_entry.__code__
        cells["entry_module"].cell_contents = fake_entry_module
        with pytest.raises(ProductCompositionError, match="inner closure target changed"):
            tick_autonomous_product_runtime(runtime)
        assert hostile_calls == 0
    finally:
        for name, original in originals.items():
            cells[name].cell_contents = original
        runtime.close()
