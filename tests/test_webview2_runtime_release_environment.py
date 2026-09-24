from __future__ import annotations

from unittest.mock import patch

import pytest

from autosport.webview2_runtime_deployment import ensure_webview2_runtime
from autosport.webview2_runtime_preflight import (
    WEBVIEW2_RELEASE_ENVIRONMENT_OVERRIDES,
    WebView2ReleaseEnvironmentError,
    evaluate_webview2_release_environment,
    probe_webview2_runtime,
    require_webview2_release_environment,
)


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
