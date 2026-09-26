from __future__ import annotations

from pathlib import Path


APP = Path(__file__).parents[1] / "src" / "autosport" / "windows_web" / "app.js"


def _source() -> str:
    return APP.read_text(encoding="utf-8")


def test_focused_operator_controls_are_not_overwritten_by_state_poll() -> None:
    source = _source()

    assert "function setValueUnlessFocused(node, value)" in source
    assert "document.activeElement !== node" in source

    guarded_projections = (
        "setValueUnlessFocused(strategy, state.strategy_id || previousStrategy);",
        "setValueUnlessFocused(byId(103), String(state.replay_speed));",
        "setValueUnlessFocused(byId(104), state.live_mode);",
        "setValueUnlessFocused(nav, state.surface_key || priorNav);",
    )
    for projection in guarded_projections:
        assert projection in source

    stale_unconditional_projections = (
        "strategy.value = state.strategy_id || previousStrategy;",
        "byId(103).value = String(state.replay_speed);",
        "byId(104).value = state.live_mode;",
        "nav.value = state.surface_key || priorNav;",
    )
    for projection in stale_unconditional_projections:
        assert projection not in source


def test_readonly_readbacks_are_not_rewritten_on_semantic_noop_poll() -> None:
    source = _source()

    assert "function setValueIfChanged(node, value)" in source
    assert "if (node.value !== text) node.value = text;" in source

    stable_readbacks = (
        'setValueIfChanged(byId(205), state.bank || "");',
        'setValueIfChanged(byId(202), (state.log || []).join("\\n"));',
        'setValueIfChanged(byId(302), surfaceDetails[0] || "СТАН: невідомий");',
        'setValueIfChanged(byId(306), state.owner.summary || "");',
        'setValueIfChanged(byId(334), state.manual.result || "");',
        'byId("product-runtime-status"),',
    )
    for projection in stable_readbacks:
        assert projection in source

    stale_unconditional_readbacks = (
        'byId(205).value = state.bank || "";',
        'byId(202).value = (state.log || []).join("\\n");',
        'byId(302).value = surfaceDetails[0] || "СТАН: невідомий";',
        'byId(306).value = state.owner.summary || "";',
        'byId(334).value = state.manual.result || "";',
        'byId("product-runtime-status").value =',
    )
    for projection in stale_unconditional_readbacks:
        assert projection not in source


def test_runtime_start_stop_transition_keeps_focus_on_an_action_or_fallback() -> None:
    source = _source()

    assert "function syncRuntimeActionAvailability(startButton, stopButton, canStart, canStop)" in source
    assert "const focused = document.activeElement;" in source
    assert "startButton.disabled = canStart !== true;" in source
    assert "stopButton.disabled = canStop !== true;" in source
    assert "focused === startButton && startButton.disabled" in source
    assert "focusOperatorTarget(stopButton);" in source
    assert "focused === stopButton && stopButton.disabled" in source
    assert "focusOperatorTarget(startButton);" in source
    assert "focused === startButton && startButton.disabled && !stopButton.disabled" not in source
    assert "focused === stopButton && stopButton.disabled && !startButton.disabled" not in source
    assert "stopButton.focus();" not in source
    assert "startButton.focus();" not in source
    assert 'byId("product-runtime-start").disabled = productRuntime.can_start !== true;' not in source
    assert 'byId("product-runtime-stop").disabled = productRuntime.can_stop !== true;' not in source
    assert "syncRuntimeActionAvailability(" in source
    assert "productRuntime.can_start," in source
    assert "productRuntime.can_stop," in source


def test_poll_driven_disable_moves_focus_to_status_or_error() -> None:
    source = _source()

    assert "function setDisabledWithFocusFallback(node, disabled)" in source
    assert "const wasFocused = document.activeElement === node;" in source
    assert "node.disabled = Boolean(disabled);" in source
    assert "const fallback = errorNode.hidden ? statusNode : errorNode;" in source
    assert "fallback.tabIndex = -1;" in source
    assert "fallback.focus();" in source

    assert "setDisabledWithFocusFallback(node, !state.owner.can_initialize);" in source
    assert "const busyDisabled = isAnyWorkerBusy(state);" in source
    assert "setDisabledWithFocusFallback(byId(id), busyDisabled);" in source
    assert "const planDisabled = busyDisabled || state.strategy_requires_plan === false;" in source
    assert "setDisabledWithFocusFallback(byId(107), planDisabled);" in source
    assert 'setDisabledWithFocusFallback(byId("research-plan-path"), planDisabled);' in source

    assert "node.disabled = !state.owner.can_initialize;" not in source
    assert "byId(id).disabled = Boolean(busy);" not in source
    assert "byId(107).disabled = true;" not in source
    assert 'byId("research-plan-path").disabled = true;' not in source


def test_focused_error_hands_off_before_the_live_region_is_hidden() -> None:
    source = _source()

    helper_start = source.index("function clearErrorStatusWithFocusHandoff()")
    helper_end = source.index("\n  function focusOperatorTarget", helper_start)
    helper = source[helper_start:helper_end]

    assert "if (document.activeElement === errorNode)" in helper
    assert "statusNode.tabIndex = -1;" in helper
    assert "statusNode.focus();" in helper
    assert "setHiddenIfChanged(errorNode, true);" in helper
    assert "setTextIfChanged(errorNode, \"\");" in helper
    assert helper.index("statusNode.focus();") < helper.index(
        "setHiddenIfChanged(errorNode, true);"
    )

    assert source.count("clearErrorStatusWithFocusHandoff();") == 2
    assert source.count("setHiddenIfChanged(errorNode, true);") == 1


def test_programmatic_focus_routes_away_from_unavailable_targets() -> None:
    source = _source()

    assert "function focusOperatorTarget(target)" in source
    assert 'target.closest("[hidden]") !== null' in source
    assert 'target.matches(":disabled")' in source
    assert "if (document.activeElement === target) return true;" in source
    assert 'const section = target.closest("section");' in source
    assert 'const sectionHeading = section ? section.querySelector("h2") : null;' in source
    assert 'sectionHeading.closest("[hidden]") === null' in source
    assert "fallback.tabIndex = -1;" in source
    assert "return document.activeElement === fallback;" in source

    assert "focusOperatorTarget(byId(result.focus_id));" in source
    assert "focusOperatorTarget(byId(selectedSurfaceTarget()));" in source
    assert "const target = byId(result.focus_id);" not in source
    assert "if (target instanceof HTMLElement) target.focus();" not in source


def test_poll_and_native_accessibility_contract_remain_intact() -> None:
    source = _source()

    assert "window.setInterval(refreshState, 250)" in source
    assert 'event.key === "F2"' in source
    assert 'event.key === "F8"' in source
    assert 'event.key === "F6"' not in source
    assert 'event.key === "F7"' not in source
    assert 'event.key === "F9"' not in source
    assert 'event.key === "F10"' not in source
    assert 'event.key.toLowerCase() === "r"' not in source
    assert "refreshInFlight" in source
    assert "refreshPending" in source
