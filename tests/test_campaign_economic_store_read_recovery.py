from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import autosport.campaign_economic_store as economic_store_module
from autosport.campaign_cost_evidence import derive_campaign_economics
from autosport.campaign_economic_store import CampaignEconomicEvidenceStore
from test_campaign_cost_evidence import T1, T2, _cost, _fixture_authority


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "economic-workspace", tmp_path / "external-monotonic-authority"


def _versions(authority):
    first_cost = _cost(authority, amount=Decimal("2"))
    first = derive_campaign_economics(
        campaign=authority,
        costs=(first_cost,),
        as_of=T1,
    )
    replacement = _cost(
        authority,
        source_digit="a",
        amount=Decimal("4"),
        available_at=T2,
        supersedes=(first_cost.cost_evidence_id,),
    )
    second = derive_campaign_economics(
        campaign=authority,
        costs=(replacement,),
        as_of=T2,
        previous=first,
    )
    return first, second


def test_latest_recovers_crash_after_version_before_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture, authority = _fixture_authority()
    try:
        workspace, authority_root = _paths(tmp_path)
        first, second = _versions(authority)
        store = CampaignEconomicEvidenceStore(
            workspace,
            campaign=authority,
            authority_root=authority_root,
        )
        store.append(first)

        original_atomic_json = economic_store_module._atomic_json
        crashed = False

        def crash_before_successor_head(path: Path, raw: dict[str, object]) -> None:
            nonlocal crashed
            if (
                not crashed
                and path.name == "head.json"
                and raw.get("version_id") == second.version_id
            ):
                crashed = True
                raise RuntimeError("injected crash after immutable successor publish")
            original_atomic_json(path, raw)

        monkeypatch.setattr(
            economic_store_module,
            "_atomic_json",
            crash_before_successor_head,
        )
        with pytest.raises(RuntimeError, match="injected crash"):
            store.append(second)
        monkeypatch.setattr(
            economic_store_module,
            "_atomic_json",
            original_atomic_json,
        )

        restarted = CampaignEconomicEvidenceStore(
            workspace,
            campaign=authority,
            authority_root=authority_root,
        )
        assert restarted.latest() == second
        assert restarted.verify_chain() == (first, second)
        assert restarted.append(second) == second.version_id
    finally:
        fixture.doCleanups()


def test_latest_recovers_crash_after_head_before_authority_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture, authority = _fixture_authority()
    try:
        workspace, authority_root = _paths(tmp_path)
        first, second = _versions(authority)
        store = CampaignEconomicEvidenceStore(
            workspace,
            campaign=authority,
            authority_root=authority_root,
        )
        store.append(first)

        original_commit = store._authority.commit
        crashed = False

        def crash_before_successor_commit(
            *,
            tx_id: str,
            observed_state_sha256: str,
            semantic_binding_sha256: str,
        ):
            nonlocal crashed
            if not crashed and observed_state_sha256 == second.version_id:
                crashed = True
                raise RuntimeError("injected crash after successor head publish")
            return original_commit(
                tx_id=tx_id,
                observed_state_sha256=observed_state_sha256,
                semantic_binding_sha256=semantic_binding_sha256,
            )

        monkeypatch.setattr(store._authority, "commit", crash_before_successor_commit)
        with pytest.raises(RuntimeError, match="injected crash"):
            store.append(second)

        restarted = CampaignEconomicEvidenceStore(
            workspace,
            campaign=authority,
            authority_root=authority_root,
        )
        assert restarted.latest() == second
        assert restarted.verify_chain() == (first, second)
        assert restarted.append(second) == second.version_id
    finally:
        fixture.doCleanups()
