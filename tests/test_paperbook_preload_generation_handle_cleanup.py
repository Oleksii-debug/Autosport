from __future__ import annotations

from decimal import Decimal

import pytest

import autosport._paperbook_preload_authority_guard as guard
from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_TS = "2026-09-27T19:36:00+00:00"


def _leg(selection_id: str) -> TicketLeg:
    return TicketLeg(
        "event-generation-handle-cleanup",
        "market-generation-handle-cleanup",
        selection_id,
        Decimal("2.5"),
        sport="soccer",
        exchange_side="back",
    )


def _bind_authority_root(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR",
        str(tmp_path.parent / f"{tmp_path.name}-generation-handle-authority"),
    )


def test_pre_generation_path_persistence_callables_are_not_module_reachable() -> None:
    assert not hasattr(guard, "_GENERATION_ORIGINAL_TRUSTED_LOAD")
    assert not hasattr(guard, "_GENERATION_ORIGINAL_TRUSTED_SAVE")

    # The owning module must retain only the composed generation-guarded public
    # persistence delegates at these canonical names.
    assert guard._trusted_path_load is not None
    assert guard._trusted_save is not None


def test_public_path_persistence_remains_functional_after_handle_cleanup(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    source = PaperBook("100")
    source.open_ticket([_leg("selection-1")], "10", placed_at=_TS)
    source.save(path)

    loaded = PaperBook.load(path)
    assert loaded.balance == Decimal("90")
    assert loaded.committed_stake == Decimal("10")

    loaded.open_ticket([_leg("selection-2")], "5", placed_at=_TS)
    loaded.save(path)

    restored = PaperBook.load(path)
    assert restored.balance == Decimal("85")
    assert restored.committed_stake == Decimal("15")


def test_canonical_publication_lock_still_blocks_public_persistence_after_cleanup(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_authority_root(tmp_path, monkeypatch)
    path = tmp_path / "paper-book.json"

    source = PaperBook("100")
    source.open_ticket([_leg("selection-lock")], "10", placed_at=_TS)
    source.save(path)
    loaded = PaperBook.load(path)

    publication_lock = guard._acquire_snapshot_publication_lock(guard._witness_path(path))
    try:
        with pytest.raises(
            ValueError,
            match="snapshot publication lock is held by another writer",
        ):
            loaded.save(path)
        with pytest.raises(
            ValueError,
            match="snapshot publication lock is held by another writer",
        ):
            PaperBook.load(path)
    finally:
        guard._release_snapshot_publication_lock(publication_lock)
