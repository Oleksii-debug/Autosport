from __future__ import annotations

import pytest

from autosport.local_compute_tariff_authority import (
    LocalComputeTariffAuthorityStore,
    LocalComputeTariffError,
)


def test_tariff_store_rejects_exact_basis_store_from_other_workspace(tmp_path) -> None:
    """An exact #1864 store from another workspace must not become tariff authority.

    Exact-type checks are insufficient here: the tariff store is required to stay
    bound to the constructor-owned allocation-basis authority for its own workspace.
    """

    canonical = LocalComputeTariffAuthorityStore(tmp_path / "canonical")
    foreign = LocalComputeTariffAuthorityStore(tmp_path / "foreign")

    canonical._basis_store = foreign._basis_store

    with pytest.raises(LocalComputeTariffError):
        canonical._basis_authority()
