from __future__ import annotations

from pathlib import Path

from autosport import cli


def test_canonical_cli_workspace_backup_restore_roundtrip(tmp_path: Path, capsys) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "user.json").write_text('{"paper":true}', encoding="utf-8")
    archive = tmp_path / "backup.zip"
    restored = tmp_path / "new-workspace"
    assert cli.main(["backup-workspace", "--workspace", str(source), "--output", str(archive)]) == 0
    assert "backup=OK" in capsys.readouterr().out
    assert cli.main(["restore-workspace", "--archive", str(archive), "--destination", str(restored)]) == 0
    assert "restore=OK" in capsys.readouterr().out
    assert (restored / "user.json").read_text() == '{"paper":true}'
    assert cli.main(["restore-workspace", "--archive", str(archive), "--destination", str(restored)]) == 3
    assert "FAIL_CLOSED" in capsys.readouterr().out
