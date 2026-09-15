from __future__ import annotations

import os
from pathlib import Path

import pytest

import autosport.evidence_export as evidence_export
import autosport.evidence_snapshot_lock as evidence_snapshot_lock


def _notify_record(action: int, name: str, *, next_offset: int = 0) -> bytes:
    encoded = name.encode("utf-16-le")
    record = (
        next_offset.to_bytes(4, "little")
        + action.to_bytes(4, "little")
        + len(encoded).to_bytes(4, "little")
        + encoded
    )
    if next_offset:
        return record + b"\x00" * (next_offset - len(record))
    return record


def test_sentinel_notification_validation_rejects_earlier_external_change() -> None:
    sentinel = "deadbeef.asv"
    external = "external.tmp"
    first_size = (12 + len(external.encode("utf-16-le")) + 3) & ~3
    payload = _notify_record(1, external, next_offset=first_size) + _notify_record(
        1,
        sentinel,
    )

    records = evidence_snapshot_lock._parse_windows_directory_changes(payload)

    with pytest.raises(
        ValueError,
        match="workspace changed during evidence snapshot namespace boundary",
    ):
        evidence_snapshot_lock._WindowsWorkspaceChangeWatch._require_only_sentinel_notifications(
            records,
            sentinel,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows V1 sentinel snapshot boundary")
def test_windows_sentinel_barrier_rejects_change_before_linearization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "paper_book.json").write_bytes(b"paper-state")
    output = tmp_path / "manifest.json"
    injected = workspace / "external.tmp"

    watch_type = evidence_snapshot_lock._WindowsWorkspaceChangeWatch
    real_create = watch_type._create_linearization_sentinel

    def create_after_external_change(self: object) -> tuple[Path, int]:
        injected.write_bytes(b"outside-change")
        return real_create(self)  # type: ignore[arg-type]

    monkeypatch.setattr(
        watch_type,
        "_create_linearization_sentinel",
        create_after_external_change,
    )

    with pytest.raises(
        ValueError,
        match="workspace changed during evidence snapshot namespace boundary",
    ):
        evidence_export.export_evidence_manifest(workspace, output)

    assert injected.read_bytes() == b"outside-change"
    assert not output.exists()
    assert not list(workspace.glob("*.asv"))


@pytest.mark.skipif(os.name != "nt", reason="Windows V1 sentinel snapshot boundary")
def test_windows_sentinel_marker_cannot_be_replaced_before_object_bound_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "paper_book.json").write_bytes(b"paper-state")
    output = tmp_path / "manifest.json"
    replacement = workspace / "replacement.tmp"
    replacement.write_bytes(b"replacement-must-survive")

    watch_type = evidence_snapshot_lock._WindowsWorkspaceChangeWatch
    real_require = watch_type._require_only_sentinel_notifications
    replacement_attempted = False

    def require_then_attempt_replacement(
        records: tuple[tuple[int, str], ...],
        sentinel_name: str,
    ) -> None:
        nonlocal replacement_attempted
        real_require(records, sentinel_name)
        sentinel = workspace / sentinel_name

        with pytest.raises(OSError):
            os.replace(replacement, sentinel)

        replacement_attempted = True
        assert sentinel.exists()
        assert replacement.read_bytes() == b"replacement-must-survive"

    monkeypatch.setattr(
        watch_type,
        "_require_only_sentinel_notifications",
        staticmethod(require_then_attempt_replacement),
    )

    evidence_export.export_evidence_manifest(workspace, output)

    assert replacement_attempted is True
    assert output.exists()
    assert replacement.read_bytes() == b"replacement-must-survive"
    assert not list(workspace.glob("*.asv"))


@pytest.mark.skipif(os.name != "nt", reason="Windows V1 sentinel snapshot boundary")
def test_windows_sentinel_barrier_cleans_marker_after_success(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "paper_book.json").write_bytes(b"paper-state")
    output = tmp_path / "manifest.json"

    evidence_export.export_evidence_manifest(workspace, output)

    assert output.exists()
    assert not list(workspace.glob("*.asv"))
