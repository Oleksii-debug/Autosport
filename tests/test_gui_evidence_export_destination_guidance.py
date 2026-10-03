from __future__ import annotations

from pathlib import Path

import pytest

from autosport.gui_evidence_export import resolve_evidence_output_destination
from autosport.localization import text


def test_evidence_export_destination_guidance_matches_ancestor_security_rule(
    tmp_path: Path,
) -> None:
    allowed_parent = tmp_path / "allowed"
    workspace = allowed_parent / "workspace"
    sibling = tmp_path / "sibling"
    workspace.mkdir(parents=True)
    sibling.mkdir()

    allowed_output = allowed_parent / "autosport-evidence.json"
    assert resolve_evidence_output_destination(workspace, allowed_output) == (
        allowed_output.resolve()
    )

    with pytest.raises(
        ValueError,
        match="output parent must be an ancestor",
    ):
        resolve_evidence_output_destination(
            workspace,
            sibling / "autosport-evidence.json",
        )

    message = text("ui.status.evidence_export.destination_invalid")
    assert "батьківській папці активної робочої області" in message
    assert "одній із папок вище" in message
    assert "Сусідні папки не підтримуються" in message
