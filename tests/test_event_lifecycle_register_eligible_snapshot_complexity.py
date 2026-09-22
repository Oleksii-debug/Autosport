from __future__ import annotations

from datetime import timedelta

from autosport.event_lifecycle import (
    CatalogEvent,
    CatalogPage,
    ContinuousEventLifecycle,
    EventPhase,
)
from autosport.storage import SQLiteMarketStore


_AS_OF = "2026-09-22T06:00:00+00:00"


def _catalog_event(index: int) -> CatalogEvent:
    return CatalogEvent(
        source_id="provider-a",
        sport="table_tennis",
        event_id=f"event-{index:04d}",
        phase=EventPhase.PRE_MATCH,
        available_at="2026-09-22T05:59:00+00:00",
        scheduled_start_at="2026-09-22T07:00:00+00:00",
    )


def _register_eligible_full_state_reads(root, *, event_count: int) -> int:
    lifecycle = ContinuousEventLifecycle(root / "catalog.json")
    lifecycle.apply_page(
        CatalogPage(
            source_id="provider-a",
            stream_epoch="epoch-1",
            cursor="cursor-1",
            position=1,
            events=tuple(_catalog_event(index) for index in range(event_count)),
        ),
        discovered_at=_AS_OF,
    )
    store = SQLiteMarketStore(root / "market.db")

    full_state_reads = 0
    original_read = lifecycle._read

    def counted_read():
        nonlocal full_state_reads
        full_state_reads += 1
        return original_read()

    # _read() parses and validates the complete lifecycle JSON document.  Count
    # calls rather than elapsed time so this regression is deterministic across
    # GitHub/Linux/Windows runners.
    lifecycle._read = counted_read  # type: ignore[method-assign]

    try:
        registered = lifecycle.register_eligible(
            store,
            as_of=_AS_OF,
            required_history=timedelta(0),
            register_input=lambda *_args, **_kwargs: None,
        )
        assert registered == ()
    finally:
        store.close()

    return full_state_reads


def test_register_eligible_uses_bounded_lifecycle_snapshot_reads(tmp_path) -> None:
    small_reads = _register_eligible_full_state_reads(
        tmp_path / "small",
        event_count=1,
    )
    larger_reads = _register_eligible_full_state_reads(
        tmp_path / "larger",
        event_count=64,
    )

    # One register_eligible() call must not repeatedly reload the same complete
    # durable lifecycle document once or twice for every event.  Keep the
    # acceptance repair-shape agnostic: a bounded number of consistency reads is
    # fine, but growing the universe by 63 events may add at most two full-state
    # reads.
    assert larger_reads <= small_reads + 2, (
        "register_eligible full-state read amplification is event-count dependent: "
        f"1 event -> {small_reads} reads; 64 events -> {larger_reads} reads"
    )
