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


def test_deleted_store_cannot_mint_weaker_owner_policy_after_prior_publication(
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

    # Lose only the mutable local risk-policy payload. The pre-delete owner
    # identity remains the externally authorized policy truth; deletion is not
    # evidence that this is a pristine workspace.
    store.path.unlink()
    assert not store.path.exists()

    with pytest.raises(PaperRiskPolicyStoreError):
        PaperRiskPolicyStore(tmp_path).initialize_owner(weaker_policy)

    # A repair may leave the local state absent and fail closed, or may restore
    # the exact prior owner from an independent canonical witness. It may never
    # publish the weaker replacement as a new owner generation.
    if store.path.exists():
        assert store.path.read_bytes() == owner_bytes
        assert PaperRiskPolicyStore(tmp_path).load(
            economic_goal=goal,
            expected_policy_provenance_sha256=owner_policy.provenance_sha256,
        ) == owner_policy
