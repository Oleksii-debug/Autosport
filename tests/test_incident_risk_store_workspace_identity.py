from __future__ import annotations

from pathlib import Path

import pytest

from autosport.incident_risk_store import IncidentRiskStore, IncidentRiskStoreError


@pytest.mark.parametrize("workspace", ["relative-workspace", Path("relative-workspace")])
def test_relative_workspace_is_rejected_before_cwd_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace: str | Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(IncidentRiskStoreError, match="workspace must be absolute"):
        IncidentRiskStore(
            workspace,
            authority_root=tmp_path / "authority",
        )

    assert not (tmp_path / "relative-workspace").exists()


def test_absolute_workspace_identity_is_preserved(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "durable-workspace"
    store = IncidentRiskStore(
        workspace,
        authority_root=tmp_path / "authority",
    )

    assert store.workspace == workspace
    assert store.path == workspace / IncidentRiskStore.FILE_NAME
