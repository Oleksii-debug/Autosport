from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

from autosport.webview2_runtime_deployment import ensure_webview2_runtime
from autosport.webview2_runtime_preflight import (
    WEBVIEW2_RELEASE_ENVIRONMENT_OVERRIDES,
    WEBVIEW2_RELEASE_POLICY_ROOT_SUBKEY,
    WebView2ReleaseEnvironmentError,
    WebView2ReleaseRegistryError,
    evaluate_webview2_release_environment,
    evaluate_webview2_release_registry,
    probe_webview2_runtime,
    require_webview2_release_environment,
    require_webview2_release_registry,
)


class _PolicyKey:
    def __init__(self, root, subkey):
        self.root = root
        self.subkey = subkey

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _policy_winreg(values=(), *, open_error=None, query_error=None):
    fake = types.ModuleType("winreg")
    fake.HKEY_LOCAL_MACHINE = "HKLM"
    fake.HKEY_CURRENT_USER = "HKCU"
    fake.KEY_READ = 0x20019
    fake.KEY_WOW64_32KEY = 0x0200
    fake.REG_SZ = 1
    configured = {
        (hive, policy, value_name): value
        for hive, policy, value_name, value in values
    }
    calls = []

    def open_key(root, subkey, reserved, access):
        calls.append(("open", root, subkey, reserved, access))
        if open_error is not None:
            raise open_error
        if subkey.startswith(WEBVIEW2_RELEASE_POLICY_ROOT_SUBKEY + "\\"):
            return _PolicyKey(root, subkey)
        raise AssertionError("runtime registration must not be reached")

    def query_value(key, value_name):
        calls.append(("query", key.root, key.subkey, value_name))
        if query_error is not None:
            raise query_error
        policy = key.subkey.rsplit("\\", 1)[-1]
        lookup = (key.root, policy, value_name)
        if lookup not in configured:
            raise FileNotFoundError("policy value missing")
        return configured[lookup], fake.REG_SZ

    fake.OpenKey = open_key
    fake.QueryValueEx = query_value
    return fake, calls


def test_clean_release_environment_is_safe_and_deterministic() -> None:
    result = evaluate_webview2_release_environment({})
    assert result.safe is True
    assert result.blocked_names == ()
    assert result.to_dict() == {
        "blocked_names": [],
        "kind": "webview2_release_environment",
        "safe": True,
        "schema_version": 1,
    }
    assert result.to_json() == (
        '{"blocked_names":[],"kind":"webview2_release_environment",'
        '"safe":true,"schema_version":1}'
    )


@pytest.mark.parametrize("name", WEBVIEW2_RELEASE_ENVIRONMENT_OVERRIDES)
def test_each_release_override_fails_closed_without_retaining_value(name: str) -> None:
    secret_value = "secret-bearing-override-value"
    result = evaluate_webview2_release_environment({name: secret_value})

    assert result.safe is False
    assert result.blocked_names == (name,)
    assert name in result.to_json()
    assert secret_value not in result.to_json()

    with pytest.raises(WebView2ReleaseEnvironmentError) as caught:
        require_webview2_release_environment({name: secret_value})
    assert name in str(caught.value)
    assert secret_value not in str(caught.value)


def test_empty_override_value_is_not_treated_as_active() -> None:
    environment = {name: "" for name in WEBVIEW2_RELEASE_ENVIRONMENT_OVERRIDES}
    assert evaluate_webview2_release_environment(environment).safe is True


def test_whitespace_override_is_active_and_rejected() -> None:
    name = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
    result = evaluate_webview2_release_environment({name: " "})
    assert result.blocked_names == (name,)
    with pytest.raises(WebView2ReleaseEnvironmentError):
        require_webview2_release_environment({name: " "})


def test_unknown_environment_names_do_not_expand_release_authority() -> None:
    result = evaluate_webview2_release_environment(
        {"AUTOSPORT_UNRELATED_TEST_SETTING": "value"}
    )
    assert result.safe is True
    assert result.blocked_names == ()


def test_non_text_override_value_is_rejected_by_pure_evaluator() -> None:
    with pytest.raises(TypeError):
        evaluate_webview2_release_environment(  # type: ignore[arg-type]
            {"PYWEBVIEW_GUI": 7}
        )


def test_canonical_live_probe_rejects_override_before_platform_or_registry(
    monkeypatch,
) -> None:
    secret_value = r"C:\\secret-bearing-runtime"
    monkeypatch.setenv("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", secret_value)

    with pytest.raises(WebView2ReleaseEnvironmentError) as caught:
        probe_webview2_runtime()

    assert "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER" in str(caught.value)
    assert secret_value not in str(caught.value)


def test_user_data_folder_override_fails_before_probe_or_installer(
    monkeypatch,
) -> None:
    secret_value = r"C:\\Users\\operator\\secret-webview-profile"
    monkeypatch.setenv("WEBVIEW2_USER_DATA_FOLDER", secret_value)

    with (
        patch(
            "autosport.webview2_runtime_deployment.probe_webview2_runtime"
        ) as probe,
        patch(
            "autosport.webview2_runtime_deployment._download_bootstrapper"
        ) as download,
        pytest.raises(WebView2ReleaseEnvironmentError) as caught,
    ):
        ensure_webview2_runtime()

    probe.assert_not_called()
    download.assert_not_called()
    assert "WEBVIEW2_USER_DATA_FOLDER" in str(caught.value)
    assert secret_value not in str(caught.value)


def test_registry_app_policy_fails_closed_without_retaining_value() -> None:
    secret = r"C:\\secret-fixed-runtime"
    fake, _calls = _policy_winreg(
        [("HKLM", "BrowserExecutableFolder", "Autosport.exe", secret)]
    )

    result = evaluate_webview2_release_registry(
        fake,
        application_id="Autosport.exe",
    )
    assert result.safe is False
    assert result.blocked_entries == (
        "HKLM:BrowserExecutableFolder:Autosport.exe",
    )
    assert secret not in result.to_json()

    with pytest.raises(WebView2ReleaseRegistryError) as caught:
        require_webview2_release_registry(
            fake,
            application_id="Autosport.exe",
        )
    assert "BrowserExecutableFolder" in str(caught.value)
    assert secret not in str(caught.value)


def test_registry_wildcard_policy_fails_closed_even_when_value_is_empty() -> None:
    fake, _calls = _policy_winreg(
        [("HKCU", "ChannelSearchKind", "*", "")]
    )
    result = evaluate_webview2_release_registry(
        fake,
        application_id="Autosport.exe",
    )
    assert result.blocked_entries == ("HKCU:ChannelSearchKind:*",)


def test_registry_policy_for_unrelated_app_does_not_block() -> None:
    fake, _calls = _policy_winreg(
        [("HKLM", "UserDataFolder", "OtherApp.exe", r"C:\\other")]
    )
    result = evaluate_webview2_release_registry(
        fake,
        application_id="Autosport.exe",
    )
    assert result.safe is True
    assert result.blocked_entries == ()


def test_registry_policy_read_error_fails_closed_without_os_detail() -> None:
    detail = "secret-bearing registry detail"
    fake, _calls = _policy_winreg(open_error=PermissionError(detail))
    with pytest.raises(WebView2ReleaseRegistryError) as caught:
        evaluate_webview2_release_registry(
            fake,
            application_id="Autosport.exe",
        )
    assert detail not in str(caught.value)


def test_live_probe_rejects_policy_before_runtime_registration() -> None:
    fake, calls = _policy_winreg(
        [("HKLM", "ReleaseChannels", "Autosport.exe", "3")]
    )
    with (
        patch.object(sys, "platform", "win32"),
        patch.object(sys, "executable", "Autosport.exe"),
        patch.dict("os.environ", {}, clear=True),
        patch.dict(sys.modules, {"winreg": fake}),
        pytest.raises(WebView2ReleaseRegistryError),
    ):
        probe_webview2_runtime()

    assert any(
        call[0] == "query" and call[2].endswith("\\ReleaseChannels")
        for call in calls
    )
    assert not any(
        "EdgeUpdate\\Clients" in call[2]
        for call in calls
        if call[0] == "open"
    )


def test_deployment_rejects_registry_policy_before_installer_download() -> None:
    fake, _calls = _policy_winreg(
        [("HKLM", "AdditionalBrowserArguments", "*", "--remote-debugging-port=9222")]
    )
    with (
        patch.object(sys, "platform", "win32"),
        patch.object(sys, "executable", "Autosport.exe"),
        patch.dict("os.environ", {}, clear=True),
        patch.dict(sys.modules, {"winreg": fake}),
        patch(
            "autosport.webview2_runtime_deployment._download_bootstrapper"
        ) as download,
        pytest.raises(WebView2ReleaseRegistryError),
    ):
        ensure_webview2_runtime()
    download.assert_not_called()


def test_deployment_rejects_override_before_runtime_probe_or_installer(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
        "--remote-debugging-port=9222",
    )
    with (
        patch(
            "autosport.webview2_runtime_deployment.probe_webview2_runtime"
        ) as probe,
        patch(
            "autosport.webview2_runtime_deployment._download_bootstrapper"
        ) as download,
        pytest.raises(WebView2ReleaseEnvironmentError),
    ):
        ensure_webview2_runtime()

    probe.assert_not_called()
    download.assert_not_called()
