import pytest

from autosport.paths import WorkspaceConfigurationError, default_workspace


@pytest.mark.parametrize("explicit_value", ["", " ", "\t", " \r\n "])
def test_explicit_blank_workspace_override_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    explicit_value: str,
) -> None:
    fallback = tmp_path / "fallback-local-app-data"
    monkeypatch.setenv("AUTOSPORT_WORKSPACE", explicit_value)
    monkeypatch.setenv("LOCALAPPDATA", str(fallback))

    with pytest.raises(WorkspaceConfigurationError):
        default_workspace()


def test_unset_workspace_override_may_use_localappdata_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    fallback = tmp_path / "fallback-local-app-data"
    monkeypatch.delenv("AUTOSPORT_WORKSPACE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(fallback))

    assert default_workspace() == fallback / "Autosport" / "workspace"
