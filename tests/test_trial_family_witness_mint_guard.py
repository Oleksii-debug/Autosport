from __future__ import annotations

import pytest

import autosport._trial_family_cross_ledger_witness as cross_ledger
import autosport._trial_family_witness_mint_guard as mint_guard

from test_trial_family_accounting import _foundation, T1


def test_generic_trial_event_append_cannot_mint_reserved_sequential_witness(tmp_path):
    _, _, _, _, _, _, store = _foundation(tmp_path)
    before = store.path.read_bytes()

    with pytest.raises(ValueError, match="reserved for product-owned cross-ledger witness"):
        store._append_event(
            cross_ledger._WITNESS_KIND,
            T1,
            {},
        )

    assert store.path.read_bytes() == before


def test_guard_module_does_not_export_pre_guard_append_capability():
    assert not hasattr(mint_guard, "_ORIGINAL_APPEND_EVENT")
    assert not hasattr(mint_guard, "_install_reserved_witness_mint_guard")
