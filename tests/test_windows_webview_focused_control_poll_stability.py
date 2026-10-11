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


def test_focused_select_options_are_not_rebuilt_by_state_poll() -> None:
    source = _source()

    helper_start = source.index("function setSelectOptions(node, options")
    helper_end = source.index("\n  function ownerValues", helper_start)
    helper = source[helper_start:helper_end]

    assert "if (document.activeElement === node) return;" in helper
    assert "while (node.options.length > projected.length)" in helper
    assert "node.appendChild(element);" in helper

    assert "setSelectOptions(strategy, state.strategy_choices || []);" in source
    assert 'setSelectOptions(nav, state.surfaces || [], "key", "title");' in source
    assert "setSelectOptions(sourceSelect, productSource.choices || []);" in source


def test_readonly_readbacks_preserve_focused_review_position_and_noop_stability() -> None:
    source = _source()

    assert "function setValueIfChanged(node, value)" in source
    assert (
        "if (document.activeElement !== node && node.value !== text) node.value = text;"
        in source
    )

    stable_readbacks = (
        'setValueIfChanged(byId(205), state.bank || "");',
        'setValueIfChanged(byId(202), (state.log || []).join("\\n"));',
        'setValueIfChanged(byId(302), surfaceDetails[0] || "СТАН: невідомий");',
        'setValueIfChanged(byId(306), state.owner.summary || "");',
        'setValueIfChanged(byId(334), state.manual.result || "");',
        'byId("product-source-status"),',
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
        'byId("product-source-status").value =',
        'byId("product-runtime-status").value =',
    )
    for projection in stale_unconditional_readbacks:
        assert projection not in source


def test_scalar_readbacks_avoid_noop_text_node_replacement() -> None:
    source = _source()

    stable_scalar_projections = (
        'setTextIfChanged(byId("workspace-value"), state.workspace || "—");',
        'setTextIfChanged(byId("active-workspace-value"), state.active_workspace || "—");',
        'setTextIfChanged(byId("dataset-summary"), state.dataset_summary || "");',
        'setTextIfChanged(byId("research-plan-summary"), state.research_plan_summary || "");',
    )
    for projection in stable_scalar_projections:
        assert projection in source

    stale_scalar_projections = (
        'byId("workspace-value").textContent = state.workspace || "—";',
        'byId("active-workspace-value").textContent = state.active_workspace || "—";',
        'byId("dataset-summary").textContent = state.dataset_summary || "";',
        'byId("research-plan-summary").textContent = state.research_plan_summary || "";',
    )
    for projection in stale_scalar_projections:
        assert projection not in source


def test_structured_readbacks_defer_poll_mutation_while_operator_focuses_them() -> None:
    source = _source()

    assert "function hasFocusedReadback(node)" in source
    assert "active === node" in source
    assert "node.contains(active)" in source
    assert 'node.tagName === "TBODY" && active === node.closest("table")' in source

    list_start = source.index("function syncTextChildren(node, values, tagName)")
    list_end = source.index("\n  function renderList", list_start)
    list_helper = source[list_start:list_end]
    assert "if (hasFocusedReadback(node)) return;" in list_helper

    table_start = source.index("function renderSingleColumnTable(body, values)")
    table_end = source.index("\n  function setSelectOptions", table_start)
    table_helper = source[table_start:table_end]
    assert "if (hasFocusedReadback(body)) return;" in table_helper

    assert 'renderList(byId(203), state.live_quotes);' in source
    assert 'renderSingleColumnTable(byId("tickets-table-body"), state.tickets);' in source
    assert 'renderList(byId(204), state.evaluation);' in source
    assert 'renderList(byId(304), surfaceDetails);' in source
    assert 'renderList(byId(307), state.owner.lines || []);' in source


def test_poll_value_projection_is_focus_safe_and_mutation_minimal() -> None:
    source = _source()

    stable_value_projections = (
        'setValueUnlessFocused(byId("dataset-path"), state.dataset_path || "");',
        'setValueUnlessFocused(byId("research-plan-path"), state.research_plan_path || "");',
        'setValueUnlessFocused(sourceSelect, productSource.selected_id);',
    )
    for projection in stable_value_projections:
        assert projection in source

    stale_value_projections = (
        'byId("dataset-path").value = state.dataset_path || "";',
        'byId("research-plan-path").value = state.research_plan_path || "";',
        'sourceSelect.value = productSource.selected_id;',
    )
    for projection in stale_value_projections:
        assert projection not in source


def test_runtime_start_stop_transition_keeps_focus_on_an_action_or_fallback() -> None:
    source = _source()

    assert "function syncRuntimeActionAvailability(startButton, stopButton, canStart, canStop)" in source
    assert "const focused = document.activeElement;" in source
    assert "const startDisabled = canStart !== true;" in source
    assert "const stopDisabled = canStop !== true;" in source
    assert "const startChanged = startButton.disabled !== startDisabled;" in source
    assert "const stopChanged = stopButton.disabled !== stopDisabled;" in source
    assert "if (startChanged) startButton.disabled = startDisabled;" in source
    assert "if (stopChanged) stopButton.disabled = stopDisabled;" in source
    assert "focused === startButton && startChanged && startDisabled" in source
    assert "focusOperatorTarget(stopButton);" in source
    assert "focused === stopButton && stopChanged && stopDisabled" in source
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
    assert "const nextDisabled = Boolean(disabled);" in source
    assert "if (node.disabled === nextDisabled) return;" in source
    assert "const wasFocused = document.activeElement === node;" in source
    assert "node.disabled = nextDisabled;" in source
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
    assert "const statusFallback = errorNode.hidden ? statusNode : errorNode;" in source
    assert "if (!(target instanceof HTMLElement)) {" in source
    assert "statusFallback.tabIndex = -1;" in source
    assert "statusFallback.focus();" in source
    assert "return document.activeElement === statusFallback;" in source
    assert 'target.closest("[hidden]") !== null' in source
    assert 'target.matches(":disabled")' in source
    assert "if (document.activeElement === target) return true;" in source
    assert 'const section = target.closest("section");' in source
    assert 'const sectionHeading = section ? section.querySelector("h2") : null;' in source
    assert 'sectionHeading.closest("[hidden]") === null' in source
    assert "fallback.tabIndex = -1;" in source
    assert "if (document.activeElement === fallback) return true;" in source
    assert "const statusFallback = errorNode.hidden ? statusNode : errorNode;" in source
    assert "statusFallback.tabIndex = -1;" in source
    assert "statusFallback.focus();" in source
    assert "return document.activeElement === statusFallback;" in source

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


def test_disclosure_close_moves_focus_before_hiding_focused_subtree() -> None:
    source = _source()

    owner_start = source.index('byId(329).addEventListener("click", () => {')
    owner_end = source.index("\n  });", owner_start)
    owner_close = source[owner_start:owner_end]
    assert owner_close.index("byId(305).focus();") < owner_close.index("ownerPanel.hidden = true;")
    assert owner_close.index("ownerPanel.hidden = true;") < owner_close.index(
        'byId(305).setAttribute("aria-expanded", "false");'
    )

    manual_start = source.index('byId(336).addEventListener("click", () => {')
    manual_end = source.index("\n  });", manual_start)
    manual_close = source[manual_start:manual_end]
    assert manual_close.index("byId(330).focus();") < manual_close.index("manualPanel.hidden = true;")
    assert manual_close.index("manualPanel.hidden = true;") < manual_close.index(
        'byId(330).setAttribute("aria-expanded", "false");'
    )
