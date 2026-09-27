(() => {
  "use strict";

  const byId = (id) => document.getElementById(String(id));
  const statusNode = byId("app-status");
  const errorNode = byId("error-status");
  const ownerPanel = byId("owner-panel");
  const manualPanel = byId("manual-panel");
  let latestState = null;
  let pollHandle = null;
  let ownerDefaultsApplied = false;
  let ownerReviewFresh = false;
  let ownerReviewEpoch = 0;
  let refreshInFlight = null;
  let refreshPending = false;
  let refreshClosed = false;
  let stateProjectionEpoch = 0;

  function setTextIfChanged(node, value) {
    const text = String(value ?? "");
    if (node.textContent !== text) node.textContent = text;
  }

  function setHiddenIfChanged(node, hidden) {
    if (node.hidden !== hidden) node.hidden = hidden;
  }

  function setValueIfChanged(node, value) {
    const text = String(value ?? "");
    if (node.value !== text) node.value = text;
  }

  function setValueUnlessFocused(node, value) {
    const text = String(value ?? "");
    if (document.activeElement !== node && node.value !== text) node.value = text;
  }

  function setDisabledWithFocusFallback(node, disabled) {
    const wasFocused = document.activeElement === node;
    node.disabled = Boolean(disabled);
    if (wasFocused && node.disabled) {
      const fallback = errorNode.hidden ? statusNode : errorNode;
      fallback.tabIndex = -1;
      fallback.focus();
    }
  }

  function clearErrorStatusWithFocusHandoff() {
    if (document.activeElement === errorNode) {
      statusNode.tabIndex = -1;
      statusNode.focus();
    }
    setHiddenIfChanged(errorNode, true);
    setTextIfChanged(errorNode, "");
  }

  function focusOperatorTarget(target) {
    if (!(target instanceof HTMLElement)) return false;
    const unavailable = (
      target.hidden
      || target.closest("[hidden]") !== null
      || target.matches(":disabled")
    );
    if (!unavailable) {
      target.focus();
      if (document.activeElement === target) return true;
    }
    const section = target.closest("section");
    const sectionHeading = section ? section.querySelector("h2") : null;
    const usableSectionHeading = (
      sectionHeading instanceof HTMLElement
      && !sectionHeading.hidden
      && sectionHeading.closest("[hidden]") === null
    );
    const fallback = usableSectionHeading
      ? sectionHeading
      : (errorNode.hidden ? statusNode : errorNode);
    fallback.tabIndex = -1;
    fallback.focus();
    return document.activeElement === fallback;
  }

  function syncRuntimeActionAvailability(startButton, stopButton, canStart, canStop) {
    const focused = document.activeElement;
    startButton.disabled = canStart !== true;
    stopButton.disabled = canStop !== true;
    if (focused === startButton && startButton.disabled) {
      focusOperatorTarget(stopButton);
    } else if (focused === stopButton && stopButton.disabled) {
      focusOperatorTarget(startButton);
    }
  }

  function isAnyWorkerBusy(state) {
    return Boolean(state && state.busy && Object.values(state.busy).some(Boolean));
  }

  function syncOwnerConfirmationAvailability(canInitialize) {
    const reviewDisabled = (
      canInitialize !== true
      || ownerReviewFresh !== true
      || isAnyWorkerBusy(latestState)
    );
    const checkbox = byId("owner-confirm-checkbox");
    setDisabledWithFocusFallback(checkbox, reviewDisabled);
    setDisabledWithFocusFallback(
      byId("owner-confirm"),
      reviewDisabled || checkbox.checked !== true,
    );
  }

  function invalidateOwnerReview() {
    ownerReviewEpoch += 1;
    ownerReviewFresh = false;
    byId("owner-confirm-checkbox").checked = false;
    syncOwnerConfirmationAvailability(
      Boolean(latestState && latestState.owner && latestState.owner.can_initialize),
    );
  }

  function markOwnerReviewFresh(epoch) {
    if (epoch !== ownerReviewEpoch) return false;
    ownerReviewFresh = true;
    byId("owner-confirm-checkbox").checked = false;
    syncOwnerConfirmationAvailability(
      Boolean(latestState && latestState.owner && latestState.owner.can_initialize),
    );
    return true;
  }

  function requestId() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
      return globalThis.crypto.randomUUID();
    }
    return "autosport-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }

  function announce(message, assertive = false) {
    const value = message || "Готово.";
    if (assertive) {
      setHiddenIfChanged(errorNode, false);
      setTextIfChanged(errorNode, value);
      return;
    }
    clearErrorStatusWithFocusHandoff();
    setTextIfChanged(statusNode, value);
  }

  function projectLiveState(status, error) {
    const errorValue = error || "";
    if (errorValue) {
      setHiddenIfChanged(errorNode, false);
      setTextIfChanged(errorNode, errorValue);
      return;
    }
    clearErrorStatusWithFocusHandoff();
    setTextIfChanged(statusNode, status || "Готово.");
  }

  function projectEmergencyStopState(emergency) {
    const node = byId("emergency-stop-status");
    const value = (
      emergency
      && typeof emergency.status === "string"
      && emergency.status
    )
      ? emergency.status
      : "Аварійний STOP: стан не підтверджено.";
    setTextIfChanged(node, value);
  }

  function syncTextChildren(node, values, tagName) {
    const projected = Array.from(values || [], (value) => String(value));
    while (node.children.length > projected.length) {
      node.removeChild(node.lastElementChild);
    }
    projected.forEach((value, index) => {
      let item = node.children[index];
      if (!item) {
        item = document.createElement(tagName);
        node.appendChild(item);
      }
      setTextIfChanged(item, value);
    });
  }

  function renderList(node, values) {
    syncTextChildren(node, values, "li");
  }

  function renderSingleColumnTable(body, values) {
    const projected = Array.from(values || [], (value) => String(value));
    while (body.rows.length > projected.length) {
      body.deleteRow(body.rows.length - 1);
    }
    projected.forEach((value, index) => {
      const row = body.rows[index] || body.insertRow();
      const cell = row.cells[0] || row.insertCell();
      setTextIfChanged(cell, value);
    });
  }

  function setSelectOptions(node, options, valueKey = "id", labelKey = "label") {
    const current = node.value;
    const projected = Array.from(options || [], (option) => ({
      value: String(option[valueKey]),
      label: String(option[labelKey]),
    }));
    while (node.options.length > projected.length) {
      node.remove(node.options.length - 1);
    }
    projected.forEach((option, index) => {
      let element = node.options[index];
      if (!element) {
        element = document.createElement("option");
        node.appendChild(element);
      }
      if (element.value !== option.value) element.value = option.value;
      setTextIfChanged(element, option.label);
    });
    if (current && Array.from(node.options).some((item) => item.value === current)) {
      node.value = current;
    }
  }

  function ownerValues() {
    const values = {};
    document.querySelectorAll("[data-owner-field]").forEach((node) => {
      values[node.dataset.ownerField] = node.value;
    });
    return values;
  }

  function applyOwnerDefaults(defaults) {
    if (ownerDefaultsApplied || !defaults) return;
    document.querySelectorAll("[data-owner-field]").forEach((node) => {
      const key = node.dataset.ownerField;
      if (Object.prototype.hasOwnProperty.call(defaults, key)) {
        node.value = String(defaults[key]);
      }
    });
    ownerDefaultsApplied = true;
  }

  function focusResult(result) {
    if (!result || !result.focus_id) return;
    focusOperatorTarget(byId(result.focus_id));
  }

  function invalidateStateProjection() {
    stateProjectionEpoch += 1;
  }

  async function apiState() {
    if (!globalThis.pywebview || !globalThis.pywebview.api) {
      throw new Error("Внутрішній канал застосунку недоступний.");
    }
    const response = await globalThis.pywebview.api.get_state();
    if (!response || response.ok !== true || !response.state) {
      throw new Error("Стан продукту недоступний.");
    }
    return response.state;
  }

  async function dispatch(actionId, payload = {}, options = {}) {
    const useGlobalAnnouncement = options.globalAnnouncement !== false;
    const useResultFocus = options.resultFocus !== false;
    const usePostRefresh = options.postRefresh !== false;
    // A state read that began before this command is not allowed to overwrite
    // the operator-visible result after the command crosses the backend bridge.
    invalidateStateProjection();
    try {
      const result = await globalThis.pywebview.api.dispatch({
        request_id: requestId(),
        action_id: actionId,
        payload,
      });
      // Also invalidate reads started while the command was in flight. The
      // shared refresh loop will skip them and obtain one post-command snapshot.
      invalidateStateProjection();
      const rejected = !result || result.status !== "completed";
      if (useGlobalAnnouncement) {
        announce(
          result && result.message ? result.message : (rejected ? "Дію відхилено." : "Готово."),
          rejected,
        );
      }
      if (useResultFocus) focusResult(result);
      if (usePostRefresh) await refreshState();
      return result;
    } catch (_error) {
      invalidateStateProjection();
      if (useGlobalAnnouncement) {
        announce("Помилка зв’язку із застосунком. Перевірте стан і повторіть дію.", true);
      }
      return null;
    }
  }

  // The dedicated emergency-STOP asset reuses this frontend ordering fence.
  // Backend emergency dispatch remains an independent safety lane.
  globalThis.autosportDispatch = dispatch;

  function renderState(state) {
    latestState = state;
    projectLiveState(state.status, state.last_error);
    projectEmergencyStopState(state.emergency_stop);

    byId("workspace-value").textContent = state.workspace || "—";
    byId("active-workspace-value").textContent = state.active_workspace || "—";
    setValueIfChanged(byId(205), state.bank || "");
    byId("dataset-summary").textContent = state.dataset_summary || "";
    if (document.activeElement !== byId("dataset-path")) {
      byId("dataset-path").value = state.dataset_path || "";
    }
    if (document.activeElement !== byId("research-plan-path")) {
      byId("research-plan-path").value = state.research_plan_path || "";
    }
    byId("research-plan-summary").textContent = state.research_plan_summary || "";

    const strategy = byId(106);
    const previousStrategy = strategy.value;
    setSelectOptions(strategy, state.strategy_choices || []);
    setValueUnlessFocused(strategy, state.strategy_id || previousStrategy);

    setValueUnlessFocused(byId(103), String(state.replay_speed));
    setValueUnlessFocused(byId(104), state.live_mode);
    setTextIfChanged(byId("live-status"), state.live_status || "");
    renderList(byId(203), state.live_quotes);
    renderSingleColumnTable(byId("tickets-table-body"), state.tickets);
    renderList(byId(204), state.evaluation);
    setValueIfChanged(byId(202), (state.log || []).join("\n"));

    const nav = byId(301);
    const priorNav = nav.value;
    setSelectOptions(nav, state.surfaces || [], "key", "title");
    setValueUnlessFocused(nav, state.surface_key || priorNav);
    const surfaceDetails = Array.from(
      state.surface_details || [],
      (value) => String(value),
    );
    setValueIfChanged(byId(302), surfaceDetails[0] || "СТАН: невідомий");
    renderList(byId(304), surfaceDetails);

    setValueIfChanged(byId(306), state.owner.summary || "");
    renderList(byId(307), state.owner.lines || []);
    renderList(byId("owner-review-list"), state.owner.review_lines || []);
    applyOwnerDefaults(state.owner.defaults);
    document.querySelectorAll("[data-owner-field], #327")
      .forEach((node) => {
        setDisabledWithFocusFallback(node, !state.owner.can_initialize);
      });
    setDisabledWithFocusFallback(
      byId(328),
      !state.owner.can_initialize || isAnyWorkerBusy(state),
    );
    syncOwnerConfirmationAvailability(state.owner.can_initialize);

    const operations = byId(331);
    if (operations.options.length === 0) {
      setSelectOptions(operations, state.manual.operations || []);
    }
    setTextIfChanged(byId("manual-status"), state.manual.status || "");
    setValueIfChanged(byId(334), state.manual.result || "");

    const productSource = state.product_source || {};
    const sourceSelect = byId("product-source-select");
    setSelectOptions(sourceSelect, productSource.choices || []);
    if (document.activeElement !== sourceSelect && productSource.selected_id) {
      sourceSelect.value = productSource.selected_id;
    }
    setValueIfChanged(
      byId("product-source-status"),
      productSource.status || "Стан джерела даних недоступний.",
    );
    setDisabledWithFocusFallback(
      sourceSelect,
      productSource.can_configure !== true,
    );
    setDisabledWithFocusFallback(
      byId("product-source-save"),
      productSource.can_configure !== true,
    );

    const productRuntime = state.product_runtime || {};
    setValueIfChanged(
      byId("product-runtime-status"),
      productRuntime.status || "Тривала симуляційна робота не запущена.",
    );
    syncRuntimeActionAvailability(
      byId("product-runtime-start"),
      byId("product-runtime-stop"),
      productRuntime.can_start,
      productRuntime.can_stop,
    );

    const busyDisabled = isAnyWorkerBusy(state);
    [101, 102, 103, 104, 105, 106, 108, 109].forEach((id) => {
      setDisabledWithFocusFallback(byId(id), busyDisabled);
    });
    const planDisabled = busyDisabled || state.strategy_requires_plan === false;
    setDisabledWithFocusFallback(byId(107), planDisabled);
    setDisabledWithFocusFallback(byId("research-plan-path"), planDisabled);
  }

  async function refreshState() {
    if (refreshClosed) return;
    if (refreshInFlight !== null) {
      refreshPending = true;
      await refreshInFlight;
      return;
    }

    refreshInFlight = (async () => {
      do {
        if (refreshClosed) {
          refreshPending = false;
          break;
        }
        refreshPending = false;
        const requestEpoch = stateProjectionEpoch;
        try {
          const state = await apiState();
          if (refreshClosed) break;
          if (requestEpoch === stateProjectionEpoch) {
            renderState(state);
          } else {
            // A mutating action crossed the bridge while this snapshot was in
            // flight. Never render that causally older view; fetch its successor.
            refreshPending = true;
          }
        } catch (_error) {
          if (refreshClosed) break;
          if (requestEpoch === stateProjectionEpoch) {
            announce("Не вдалося оновити стан застосунку.", true);
          } else {
            // A failure from a causally stale poll is stale presentation too.
            // Do not overwrite a newer action result with an obsolete error.
            refreshPending = true;
          }
        }
      } while (refreshPending && !refreshClosed);
    })();

    try {
      await refreshInFlight;
    } finally {
      refreshInFlight = null;
    }
  }

  function selectedSurfaceTarget() {
    const mapping = {
      home_dashboard: 205,
      market_mirror: 104,
      research_agents: 106,
      portfolio: 204,
      paper_bank: 205,
      tickets_positions: 201,
      evaluation_learning: 204,
      history_results: 202,
      settings: 305,
      manual_calculation: 330,
      diagnostics_recovery: 108,
      help_about: "help-heading",
    };
    return mapping[byId(301).value] || 304;
  }

  byId(101).addEventListener("click", () => {
    dispatch("dataset.select", { path: byId("dataset-path").value });
  });
  byId(106).addEventListener("change", () => {
    invalidateOwnerReview();
    dispatch("strategy.set", { strategy_id: byId(106).value });
  });
  byId(107).addEventListener("click", () => {
    invalidateOwnerReview();
    dispatch("research_plan.select", { path: byId("research-plan-path").value });
  });
  byId(103).addEventListener("change", () => {
    dispatch("replay_speed.set", { speed: Number(byId(103).value) });
  });
  byId(104).addEventListener("change", () => {
    dispatch("live_mode.set", { mode: byId(104).value });
  });
  byId(102).addEventListener("click", () => {
    dispatch("replay.run", {
      dataset_path: byId("dataset-path").value,
      research_plan_path: byId("research-plan-path").value,
    });
  });
  byId(105).addEventListener("click", () => dispatch("live.refresh"));
  byId(108).addEventListener("click", () => dispatch("recovery.run"));
  byId(109).addEventListener("click", () => {
    dispatch("evidence.export", { path: byId("evidence-path").value });
  });
  byId("product-source-save").addEventListener("click", () => {
    dispatch("product_source.configure", {
      source_id: byId("product-source-select").value,
    });
  });
  byId("product-runtime-start").addEventListener("click", () => {
    dispatch("product_runtime.start");
  });
  byId("product-runtime-stop").addEventListener("click", () => {
    dispatch("product_runtime.stop");
  });

  byId(301).addEventListener("change", () => {
    dispatch("surface.select", { surface_key: byId(301).value });
  });
  byId(303).addEventListener("click", () => {
    focusOperatorTarget(byId(selectedSurfaceTarget()));
  });

  byId(305).addEventListener("click", () => {
    ownerPanel.hidden = false;
    byId(305).setAttribute("aria-expanded", "true");
    byId(306).focus();
  });
  byId(329).addEventListener("click", () => {
    ownerPanel.hidden = true;
    byId(305).setAttribute("aria-expanded", "false");
    byId(305).focus();
  });
  document.querySelectorAll("[data-owner-field]").forEach((node) => {
    node.addEventListener("input", invalidateOwnerReview);
    node.addEventListener("change", invalidateOwnerReview);
  });
  byId(327).addEventListener("change", invalidateOwnerReview);
  byId("owner-confirm-checkbox").addEventListener("change", () => {
    syncOwnerConfirmationAvailability(
      Boolean(latestState && latestState.owner && latestState.owner.can_initialize),
    );
  });
  byId(328).addEventListener("click", async () => {
    invalidateOwnerReview();
    const reviewEpoch = ownerReviewEpoch;
    const result = await dispatch("owner.preview", {
      values: ownerValues(),
      emergency_stop: byId(327).checked,
    });
    if (result && result.status === "completed") {
      markOwnerReviewFresh(reviewEpoch);
    }
  });
  byId("owner-confirm").addEventListener("click", async () => {
    if (ownerReviewFresh !== true || byId("owner-confirm-checkbox").checked !== true) {
      return;
    }
    const result = await dispatch("owner.initialize", {
      values: ownerValues(),
      emergency_stop: byId(327).checked,
      confirmed: true,
    });
    if (result && result.status === "completed") {
      invalidateOwnerReview();
    }
  });

  byId(330).addEventListener("click", () => {
    manualPanel.hidden = false;
    byId(330).setAttribute("aria-expanded", "true");
    byId(331).focus();
  });
  byId(336).addEventListener("click", () => {
    manualPanel.hidden = true;
    byId(330).setAttribute("aria-expanded", "false");
    byId(330).focus();
  });
  byId(333).addEventListener("click", () => {
    dispatch("manual.calculate", {
      operation: byId(331).value,
      input: byId(332).value,
    });
  });
  byId(335).addEventListener("click", async () => {
    byId(332).value = "";
    await dispatch("manual.clear");
    byId(332).focus();
  });

  function hasShortcutModifier(event) {
    return (
      event.altKey
      || event.ctrlKey
      || event.metaKey
      || event.shiftKey
    );
  }

  document.addEventListener("keydown", (event) => {
    if (hasShortcutModifier(event)) return;
    if (event.key === "F2") {
      event.preventDefault();
      byId(301).focus();
      return;
    }
    if (event.key === "F8") {
      event.preventDefault();
      byId(204).focus();
    }
  });

  window.addEventListener("pywebviewready", async () => {
    await refreshState();
    byId(301).focus();
    pollHandle = window.setInterval(refreshState, 250);
  });

  window.addEventListener("beforeunload", () => {
    refreshClosed = true;
    refreshPending = false;
    if (pollHandle !== null) window.clearInterval(pollHandle);
  });
})();