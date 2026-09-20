from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import shutil

import pytest

import autosport.campaign_economic_store as economic_store_module
from autosport.campaign_cost_evidence import (
    EconomicCompleteness,
    derive_campaign_economics,
)
from autosport.campaign_economic_store import (
    CampaignEconomicEvidenceStore,
    CampaignEconomicStoreError,
)
from autosport.monotonic_workspace_authority import MonotonicAuthorityRollbackError
from test_campaign_cost_evidence import T1, T2, _cost, _fixture_authority


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "economic-workspace", tmp_path / "external-monotonic-authority"


def test_store_exact_retry_restart_and_chain_roundtrip(tmp_path: Path) -> None:
    fixture, authority = _fixture_authority()
    try:
        workspace, authority_root = _paths(tmp_path)
        first_cost = _cost(authority, amount=Decimal("2"))
        first = derive_campaign_economics(
            campaign=authority,
            costs=(first_cost,),
            as_of=T1,
        )
        replacement = _cost(
            authority,
            source_digit="f",
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

        store = CampaignEconomicEvidenceStore(
            workspace,
            campaign=authority,
            authority_root=authority_root,
        )
        assert store.append(first) == first.version_id
        assert store.append(first) == first.version_id
        assert store.append(second) == second.version_id
        assert store.latest() == second
        assert store.load(first.version_id) == first
        assert store.verify_chain() == (first, second)

        restarted = CampaignEconomicEvidenceStore(
            workspace,
            campaign=authority,
            authority_root=authority_root,
        )
        assert restarted.latest() == second
        assert restarted.verify_chain() == (first, second)
    finally:
        fixture.doCleanups()


def test_successor_retry_recovers_crash_after_version_before_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture, authority = _fixture_authority()
    try:
        workspace, authority_root = _paths(tmp_path)
        first_cost = _cost(authority, amount=Decimal("2"))
        first = derive_campaign_economics(
            campaign=authority,
            costs=(first_cost,),
            as_of=T1,
        )
        replacement = _cost(
            authority,
            source_digit="d",
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
        assert restarted.append(second) == second.version_id
        assert restarted.latest() == second
        assert restarted.verify_chain() == (first, second)
    finally:
        fixture.doCleanups()


def test_store_rederives_and_rejects_forged_complete_version(tmp_path: Path) -> None:
    fixture, authority = _fixture_authority()
    try:
        workspace, authority_root = _paths(tmp_path)
        cost = _cost(authority)
        valid = derive_campaign_economics(
            campaign=authority,
            costs=(cost,),
            as_of=T2,
        )
        forged = replace(
            valid,
            known_cost_total=Decimal("2"),
            net_after_known_costs=Decimal("58"),
            completeness=EconomicCompleteness.COMPLETE_NET_ECONOMICS,
            incomplete_reasons=(),
        )
        store = CampaignEconomicEvidenceStore(
            workspace,
            campaign=authority,
            authority_root=authority_root,
        )
        with pytest.raises(CampaignEconomicStoreError, match="forged"):
            store.append(forged)
        assert store.latest() is None
    finally:
        fixture.doCleanups()


def test_tampered_immutable_version_fails_integrity_validation(tmp_path: Path) -> None:
    fixture, authority = _fixture_authority()
    try:
        workspace, authority_root = _paths(tmp_path)
        version = derive_campaign_economics(
            campaign=authority,
            costs=(_cost(authority),),
            as_of=T2,
        )
        store = CampaignEconomicEvidenceStore(
            workspace,
            campaign=authority,
            authority_root=authority_root,
        )
        store.append(version)
        path = (
            workspace
            / "campaign_economics"
            / version.campaign_sha256
            / "versions"
            / f"{version.version_id}.json"
        )
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["known_cost_total"] = "999"
        path.write_text(
            json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(CampaignEconomicStoreError, match="integrity validation"):
            CampaignEconomicEvidenceStore(
                workspace,
                campaign=authority,
                authority_root=authority_root,
            ).latest()
    finally:
        fixture.doCleanups()


def test_full_local_workspace_rollback_is_rejected_by_external_authority(tmp_path: Path) -> None:
    fixture, authority = _fixture_authority()
    try:
        workspace, authority_root = _paths(tmp_path)
        first_cost = _cost(authority, amount=Decimal("2"))
        first = derive_campaign_economics(
            campaign=authority,
            costs=(first_cost,),
            as_of=T1,
        )
        replacement = _cost(
            authority,
            source_digit="e",
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
        store = CampaignEconomicEvidenceStore(
            workspace,
            campaign=authority,
            authority_root=authority_root,
        )
        store.append(first)

        backup = tmp_path / "v1-workspace-snapshot"
        shutil.copytree(workspace, backup)

        store.append(second)
        assert store.latest() == second

        shutil.rmtree(workspace)
        shutil.copytree(backup, workspace)

        rolled_back = CampaignEconomicEvidenceStore(
            workspace,
            campaign=authority,
            authority_root=authority_root,
        )
        with pytest.raises(MonotonicAuthorityRollbackError):
            rolled_back.latest()
    finally:
        fixture.doCleanups()


def test_deleting_local_head_after_commit_cannot_reset_to_pristine(tmp_path: Path) -> None:
    fixture, authority = _fixture_authority()
    try:
        workspace, authority_root = _paths(tmp_path)
        version = derive_campaign_economics(
            campaign=authority,
            costs=(),
            as_of=T2,
        )
        store = CampaignEconomicEvidenceStore(
            workspace,
            campaign=authority,
            authority_root=authority_root,
        )
        store.append(version)
        head = workspace / "campaign_economics" / version.campaign_sha256 / "head.json"
        head.unlink()
        version_path = (
            workspace
            / "campaign_economics"
            / version.campaign_sha256
            / "versions"
            / f"{version.version_id}.json"
        )
        version_path.unlink()

        with pytest.raises(MonotonicAuthorityRollbackError):
            CampaignEconomicEvidenceStore(
                workspace,
                campaign=authority,
                authority_root=authority_root,
            ).latest()
    finally:
        fixture.doCleanups()
