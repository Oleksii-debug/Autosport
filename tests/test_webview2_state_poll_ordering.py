from __future__ import annotations

import re
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_APP_JS = _ROOT / "src" / "autosport" / "windows_web" / "app.js"
_SHELL = _ROOT / "src" / "autosport" / "windows_webview_shell.py"


def _javascript_function_body(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.find(marker)
    assert start >= 0, f"missing JavaScript function {name}"
    brace = source.find("{", start)
    assert brace >= 0

    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[brace + 1 : index]
    raise AssertionError(f"unterminated JavaScript function {name}")


def _has_shared_single_flight_refresh(source: str) -> bool:
    body = _javascript_function_body(source, "refreshState")
    candidates = re.findall(
        r"\blet\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:null|false)\s*;",
        source,
    )
    for name in candidates:
        lowered = name.lower()
        if not any(token in lowered for token in ("refresh", "poll", "inflight", "pending")):
            continue
        if name not in body:
            continue
        # A shared in-flight guard must be both observed and cleared by the
        # refresh function itself so timer- and dispatch-triggered refreshes
        # cannot create independent overlapping get_state calls.
        if "finally" in body and re.search(
            rf"\b{re.escape(name)}\b\s*=\s*(?:null|false)",
            body,
        ):
            return True
    return False


def _has_monotonic_state_revision_fence(
    javascript: str,
    shell_source: str,
) -> bool:
    if "state_revision" not in javascript or "state_revision" not in shell_source:
        return False

    body = _javascript_function_body(javascript, "refreshState")
    render_position = body.find("renderState")
    revision_position = body.find("state_revision")
    if revision_position < 0 or render_position < 0 or revision_position > render_position:
        return False

    prefix = body[:render_position]
    # The exact implementation is intentionally not prescribed, but a revision
    # path must compare the candidate revision with previously applied state
    # before renderState can mutate operator-visible controls/readback.
    return bool(re.search(r"(?:<=|>=|<|>)", prefix))


def test_webview2_state_refresh_has_ordering_fence_for_background_and_dispatch_calls() -> None:
    javascript = _APP_JS.read_text(encoding="utf-8")
    shell_source = _SHELL.read_text(encoding="utf-8")

    assert "await globalThis.pywebview.api.get_state()" in javascript
    assert "await refreshState()" in javascript
    assert "setInterval(refreshState, 250)" in javascript

    single_flight = _has_shared_single_flight_refresh(javascript)
    monotonic_revision = _has_monotonic_state_revision_fence(
        javascript,
        shell_source,
    )

    assert single_flight or monotonic_revision, (
        "WebView2 state projection has no ordering authority: background "
        "setInterval refreshes and dispatch-triggered refreshes can overlap. "
        "Require one shared single-flight get_state fence or a product-owned "
        "monotonic state_revision that is rejected before stale renderState()."
    )


def test_state_ordering_fence_covers_operator_enablement_and_runtime_truth() -> None:
    javascript = _APP_JS.read_text(encoding="utf-8")
    shell_source = _SHELL.read_text(encoding="utf-8")

    # These are the safety-relevant surfaces that stale state must never roll
    # backwards after a newer snapshot has already been presented.
    for required_projection in (
        'byId("product-runtime-start").disabled',
        'byId("product-runtime-stop").disabled',
        "state.busy",
        "state.product_runtime",
        "state.tickets",
        "state.evaluation",
    ):
        assert required_projection in javascript

    assert _has_shared_single_flight_refresh(
        javascript
    ) or _has_monotonic_state_revision_fence(javascript, shell_source)
