from __future__ import annotations

import os


WEBVIEW2_ENVIRONMENT_OVERRIDES = (
    "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER",
    "WEBVIEW2_USER_DATA_FOLDER",
    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
    "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE",
    "WEBVIEW2_CHANNEL_SEARCH_KIND",
    "WEBVIEW2_RELEASE_CHANNELS",
    "WEBVIEW2_WAIT_FOR_SCRIPT_DEBUGGER",
    "WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER",
)


def active_webview2_environment_overrides() -> tuple[str, ...]:
    """Return release-sensitive WebView2 environment overrides that are actually set.

    Exact empty strings are treated as unset because WebView2 does not receive an
    override value from them. Whitespace is deliberately not normalized: it remains
    externally supplied process state and therefore fails closed.
    """

    return tuple(
        name
        for name in WEBVIEW2_ENVIRONMENT_OVERRIDES
        if (value := os.environ.get(name)) is not None and value != ""
    )
