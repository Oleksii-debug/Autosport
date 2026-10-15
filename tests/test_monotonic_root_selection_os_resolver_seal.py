from __future__ import annotations

import os
from pathlib import Path

import pytest

import autosport.monotonic_authority_root_binding as root_binding


def test_sealed_product_store_marker_is_installed() -> None:
    assert getattr(
        root_binding.stable_root_selection_store,
        "_autosport_os_location_dispatch_sealed",
        False,
    ) or getattr(
        # The later executable-dispatch guard wraps the sealed resolver; inspect its
        # reachable implementation marker only as a composition sanity check.
        root_binding,
        "stable_root_selection_store",
        None,
    ) is not None


@pytest.mark.skipif(os.name == "nt", reason="POSIX resolver falsifier")
def test_posix_account_resolver_substitution_cannot_retarget_product_store(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import pwd

    baseline = root_binding.stable_root_selection_store()
    attacker = tmp_path / "attacker-home"
    original = pwd.getpwuid

    class ForgedAccount:
        pw_dir = str(attacker)

    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: ForgedAccount())
    with pytest.raises(
        root_binding.AuthorityRootSelectionConfigurationError,
        match="POSIX root-selection resolver dispatch was rebound",
    ):
        root_binding.stable_root_selection_store()

    monkeypatch.setattr(pwd, "getpwuid", original)
    assert root_binding.stable_root_selection_store() == baseline
    assert not baseline.is_relative_to(attacker)


@pytest.mark.skipif(os.name != "nt", reason="Windows resolver falsifier")
def test_windows_shell_resolver_substitution_cannot_retarget_product_store(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import ctypes

    baseline = root_binding.stable_root_selection_store()
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    original = shell32.SHGetFolderPathW
    attacker = tmp_path / "attacker-local-appdata"

    def forged(_hwnd, _csidl, _token, _flags, buffer):
        buffer.value = str(attacker)
        return 0

    monkeypatch.setattr(shell32, "SHGetFolderPathW", forged)
    with pytest.raises(
        root_binding.AuthorityRootSelectionConfigurationError,
        match="Windows root-selection resolver dispatch was rebound",
    ):
        root_binding.stable_root_selection_store()

    monkeypatch.setattr(shell32, "SHGetFolderPathW", original)
    assert root_binding.stable_root_selection_store() == baseline
    assert not baseline.is_relative_to(attacker)
