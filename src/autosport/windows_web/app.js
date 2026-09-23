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
  let refreshInFlight = null;
  let refreshPending = false;

  function setTextIfChanged(node, value) {
    const text = String(value ?? "");
    if (node.textContent !== text) node.textContent = text;
  }

  function setHiddenIfChanged(node, hidden) {
    if (node.hidden !== hidden) node.hidden = hidden;
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
    setHiddenIfChanged(errorNode, true);
    setTextIfChanged(errorNode, "");
    setTextIfChanged(statusNode, value);
  }

  function projectLiveState(status, error) {
    const errorValue = error || "";
    if (errorValue) {
      setHiddenIfChanged(errorNode, false);
      setTextIfChanged(errorNode, errorValue);
      return;
    }
    setHiddenIfChanged(errorNode, true);
    setTextIfChanged(errorNode, "");
    setTextIfChanged(statusNode, status || "Готово.");
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
    const target = byId(result.focus_id);
    if (target instanceof HTMLElement) target.focus();
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

  async function dispatch(actionId, payload = {}) {
    try {
      const result = await globalThis.pywebview.api.dispatch({
        request_id: requestId(),
        action_id: actionId,
        payload,
      });
      const rejected = !result || result.status !== "completed";
      announce(result && result.message ? result.message : (rejected ? "Дію відхилено." : "Готово."), rejected);
      focusResult(result);
      await refreshState();
      return result;
    } catch (_error) {
      announce("Помилка зв’язку із застосунком. Перевірте стан і повторіть дію.", true);
      return null;
    }
  }

  function renderState(state) {
    latestState = state;
    projectLiveState(state.status, state.last_error);

    byId("workspace-value").textContent = state.workspace || "—";
    byId("active-workspace-value").textContent = state.active_workspace || "—";
    byId(205).value = state.bank || "";
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
    strategy.value = state.strategy_id || previousStrategy;

    byId(103).value = String(state.replay_speed);
    byId(104).value = state.live_mode;
    setTextIfChanged(byId("live-status"), state.live_status || "");
    renderList(byId(203), state.live_quotes);
    renderSingleColumnTable(byId("tickets-table-body"), state.tickets);
    renderList(byId(204), state.evaluation);
    byId(202).value = (state.log || []).join("\n");

    const nav = byId(301);
    const priorNav = nav.value;
    setSelectOptions(nav, state.surfaces || [], "key", "title");
    nav.value = state.surface_key || priorNav;
    byId(302).value = state.surface_state || "";
    renderList(byId(304), state.surface_details);

    byId(306).value = state.owner.summary || "";
    renderList(byId(307), state.owner.lines || []);
    renderList(byId("owner-review-list"), state.owner.review_lines || []);
    applyOwnerDefaults(state.owner.defaults);
    document.querySelectorAll("[data-owner-field], #327, #328, #owner-confirm-checkbox, #owner-confirm")
      .forEach((node) => {
        node.disabled = !state.owner.can_initialize;
      });

    const operations = byId(331);
    if (operations.options.length === 0) {
      setSelectOptions(operations, state.manual.operations || []);
    }
    setTextIfChanged(byId("manual-status"), state.manual.status || "");
    byId(334).value = state.manual.result || "";

    const productRuntime = state.product_runtime || {};
    byId("product-runtime-status").value = productRuntime.status || "Тривала симуляційна робота не запущена.";
    byId("product-runtime-start").disabled = productRuntime.can_start !== true;
    byId("product-runtime-stop").disabled = productRuntime.can_stop !== true;

    const busy = state.busy && Object.values(state.busy).some(Boolean);
    [101, 102, 103, 104, 105, 106, 107, 108, 109].forEach((id) => {
      byId(id).disabled = Boolean(busy);
    });
    if (state.strategy_requires_plan === false) {
      byId(107).disabled = true;
      byId("research-plan-path").disabled = true;
    } else {
      byId("research-plan-path").disabled = Boolean(busy);
    }
  }

  async function refreshState() {
    if (refreshInFlight !== null) {
      refreshPending = true;
      await refreshInFlight;
      return;
    }

    refreshInFlight = (async () => {
      do {
        refreshPending = false;
        try {
          renderState(await apiState());
        } catch (_error) {
          announce("Не вдалося оновити стан застосунку.", true);
        }
      } while (refreshPending);
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
    dispatch("strategy.set", { strategy_id: byId(106).value });
  });
  byId(107).addEventListener("click", () => {
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
    const target = byId(selectedSurfaceTarget());
    if (target instanceof HTMLElement) target.focus();
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
  byId(328).addEventListener("click", () => {
    dispatch("owner.preview", {
      values: ownerValues(),
      emergency_stop: byId(327).checked,
    });
  });
  byId("owner-confirm").addEventListener("click", () => {
    dispatch("owner.initialize", {
      values: ownerValues(),
      emergency_stop: byId(327).checked,
      confirmed: byId("owner-confirm-checkbox").checked,
    });
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

  document.addEventListener("keydown", (event) => {
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
    byId("main-content").focus();
    pollHandle = window.setInterval(refreshState, 250);
  });

  window.addEventListener("beforeunload", () => {
    if (pollHandle !== null) window.clearInterval(pollHandle);
  });
})();
