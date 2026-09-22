from __future__ import annotations

import pytest

import autosport.product_runtime as product_runtime


class _Source:
    source_id = "construction-cleanup-test"

    def resolve_event(self, delta):  # pragma: no cover - build fails before use
        raise AssertionError("resolve_event must not be called in this test")


class _FakeStore:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_build_closes_market_store_if_mirror_construction_fails(
    tmp_path, monkeypatch
) -> None:
    store = _FakeStore()
    monkeypatch.setattr(product_runtime, "SQLiteMarketStore", lambda path: store)

    def fail_mirror():
        raise RuntimeError("mirror construction failed")

    monkeypatch.setattr(product_runtime, "MarketMirror", fail_mirror)

    with pytest.raises(RuntimeError, match="mirror construction failed"):
        product_runtime.build_autonomous_product_runtime(
            workspace=tmp_path,
            source=_Source(),
            initial_bankroll="100",
        )

    assert store.closed is True
