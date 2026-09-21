from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.paper_risk_policy_store import (
    PaperRiskPolicyStore,
    PaperRiskPolicyStoreError,
)
from autosport.risk import PaperRiskPolicy


_BASE_PATH = Path(__file__).with_name("test_paper_risk_policy_store.py")
_SPEC = importlib.util.spec_from_file_location(
    "_paper_risk_policy_parent_tests_for_rollback",
    _BASE_PATH,
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("cannot load paper risk policy parent fixtures")
_base = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_base)


def _weaker_policy(goal):
    return PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.020"),
        max_committed_fraction=Decimal("0.150"),
        minimum_cash_reserve_fraction=Decimal("0.250"),
        economic_goal=goal,
    )


def test_deleted_store_cannot_mint_new_owner_policy_after_prior_publication(
    tmp_path,
) -> None:
    goal = _base._goal()
    owner_policy = _base._policy(goal)
    weaker_policy = _weaker_policy(goal)
    store = PaperRiskPolicyStore(tmp_path)

    store.initialize_owner(owner_policy)
    owner_bytes = store.path.read_bytes()
    assert store.load(
        economic_goal=goal,
        expected_policy_provenance_sha256=owner_policy.provenance_sha256,
    ) == owner_policy

    # Simulate loss/rollback of only the mutable local policy file. The caller
    # still holds the previously authorized owner provenance and the workspace
    # itself is not a new product identity.
    store.path.unlink()
    assert not store.path.exists()

    with pytest.raises(
        PaperRiskPolicyStoreError,
        match="",
    ):
        PaperRiskPolicyStore(tmp_path).initialize_owner(weaker_policy)

    # Rejected rebootstrap must not publish the weaker owner policy. A repair may
    # either leave the missing state fail-closed or deterministically restore the
    # exact prior owner from an independent canonical authority.
    if store.path.exists():
        assert store.path.read_bytes() == owner_bytes
        assert PaperRiskPolicyStore(tmp_path).load(
            economic_goal=goal,
            expected_policy_provenance_sha256=owner_policy.provenance_sha256,
        ) == owner_policy


def test_deleted_store_cannot_reissue_same_policy_as_a_new_owner_generation(
    tmp_path,
) -> None:
    goal = _base._goal()
    owner_policy = _base._policy(goal)
    store = PaperRiskPolicyStore(tmp_path)
    store.initialize_owner(owner_policy)
    owner_bytes = store.path.read_bytes()

    store.path.unlink()
    assert not store.path.exists()

    with pytest.raises(
        PaperRiskPolicyStoreError,
        match="",
    ):
        PaperRiskPolicyStore(tmp_path).initialize_owner(owner_policy)

    # Exact-policy recovery, if supported later, must be reconstruction from the
    # surviving authority, not a fresh initialize_owner issuance.
    if store.path.exists():
        assert store.path.read_bytes() == owner_bytes


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
