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
  const ownerEditedFields = new WeakSet();
  let ownerReviewFresh = false;
  let ownerReviewEpoch = 0;
  let refreshInFlight = null;
  let refreshPending = false;
  let refreshClosed = false;
  let stateProjectionEpoch = 0;
  // A delayed bridge reply may restore focus only if the operator has not moved.
  let operatorFocusEpoch = 0;
  document.addEventListener("focusin", () => {
    operatorFocusEpoch += 1;
  }, true);
  // The backend may accept an operation before WebView loses the response.
  // Only explicit operator acknowledgment dismisses the UNKNOWN warning.
  let unconfirmedDispatchResult = false;
  const dispatchUncertainAck = byId("dispatch-uncertain-ack");

  function setTextIfChanged(node, value) {
    const text = String(value ?? "");
    if (node.textContent !== text) node.textContent = text;
  }

  function setHiddenIfChanged(node, hidden) {
    if (node.hidden !== hidden) node.hidden = hidden;
  }

  function setValueIfChanged(node, value) {
    const text = String(value ?? "");
    if (document.activeElement !== node && node.value !== text) node.value = text;
  }

  function setValueUnlessFocused(node, value) {
    const text = String(value ?? "");
    if (document.activeElement !== node && node.value !== text) node.value = text;
  }

  // The 250 ms snapshot poll must not erase a typed but unsubmitted file path
  // just because the operator pressed Tab to reach its Save button.
  const pathDraftEdits = new Map();
  const unsavedPathDrafts = new Set();
  let pendingDatasetValidation = null;
  for (const id of ["dataset-path", "research-plan-path"]) {
    pathDraftEdits.set(id, 0);
    byId(id).addEventListener("input", () => {
      unsavedPathDrafts.add(id);
      pathDraftEdits.set(id, pathDraftEdits.get(id) + 1);
      if (id === "dataset-path") pendingDatasetValidation = null;
    });
  }

  function renderCanonicalPathUnlessDraft(id, value) {
    if (!unsavedPathDrafts.has(id)) {
      setValueUnlessFocused(byId(id), value);
    }
  }

  function finalizeConfirmedPathSelection(id, editEpoch, result) {
    if (id === "dataset-path") {
      // Scheduling the dataset worker is not successful validation.
      const pending = result && result.dataset_validation_pending;
      if (result && result.status === "completed"
          && pending && typeof pending.path === "string" && pending.path
          && Number.isSafeInteger(pending.after_generation)
          && pending.after_generation >= 0
          && pathDraftEdits.get(id) === editEpoch) {
        pendingDatasetValidation = {
          path: pending.path,
          afterGeneration: pending.after_generation,
          editEpoch,
        };
      }
      return;
    }
    // Research-plan binding completes synchronously on the server.
    if (result && result.status === "completed"
        && pathDraftEdits.get(id) === editEpoch) {
      unsavedPathDrafts.delete(id);
    }
  }

  function confirmValidatedDatasetSelection(state) {
    const pending = pendingDatasetValidation;
    if (pending === null
        || !unsavedPathDrafts.has("dataset-path")
        || pathDraftEdits.get("dataset-path") !== pending.editEpoch
        || !Number.isSafeInteger(state.dataset_success_generation)
        || state.dataset_success_generation <= pending.afterGeneration
        || state.dataset_path !== pending.path) return;
    unsavedPathDrafts.delete("dataset-path");
    pendingDatasetValidation = null;
  }

  // A provider/source choice is a pending operator draft, not canonical truth.
  // In particular, an unconfigured source has selected_id="", so the native
  // select must not silently show the first real source as already selected.
  const sourceSelectionDraft = { epoch: 0, dirty: false };
  byId("product-source-select").addEventListener("change", () => {
    sourceSelectionDraft.epoch += 1;
    sourceSelectionDraft.dirty = true;
  });

  function hasFocusedReadback(node) {
    const active = document.activeElement;
    if (!(active instanceof HTMLElement)) return false;
    return (
      active === node
      || node.contains(active)
      || (node.tagName === "TBODY" && active === node.closest("table"))
    );
  }

  function setDisabledWithFocusFallback(node, disabled) {
    const nextDisabled = Boolean(disabled);
    if (node.disabled === nextDisabled) return;
    const wasFocused = document.activeElement === node;
    node.disabled = nextDisabled;
    if (wasFocused && nextDisabled) {
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
    const statusFallback = errorNode.hidden ? statusNode : errorNode;
    if (!(target instanceof HTMLElement)) {
      statusFallback.tabIndex = -1;
      statusFallback.focus();
      return document.activeElement === statusFallback;
    }
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
    // Plain section headings are not natively focusable. Make the fallback
    // programmatically focusable without adding it to the Tab order so a hidden
    // or disabled requested destination never leaves keyboard/NVDA focus behind.
    fallback.tabIndex = -1;
    fallback.focus();
    if (document.activeElement === fallback) return true;

    // If the section heading cannot accept focus for any host/browser reason,
    // retain one deterministic accessible fallback rather than silently keeping
    // focus on the unavailable control.
    statusFallback.tabIndex = -1;
    statusFallback.focus();
    return document.activeElement === statusFallback;
  }

  function syncRuntimeActionAvailability(startButton, stopButton, canStart, canStop) {
    const focused = document.activeElement;
    const startDisabled = canStart !== true;
    const stopDisabled = canStop !== true;
    const startChanged = startButton.disabled !== startDisabled;
    const stopChanged = stopButton.disabled !== stopDisabled;
    if (startChanged) startButton.disabled = startDisabled;
    if (stopChanged) stopButton.disabled = stopDisabled;
    if (focused === startButton && startChanged && startDisabled) {
      focusOperatorTarget(stopButton);
    } else if (focused === stopButton && stopChanged && stopDisabled) {
      focusOperatorTarget(startButton);
    }
  }

  function isAnyWorkerBusy(state) {
    return Boolean(
      state
      && (
        state.closing === true
        || (state.busy && Object.values(state.busy).some(Boolean))
      )
    );
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

  function markOwnerFieldEdited(event) {
    const node = event.currentTarget;
    if (node instanceof HTMLElement) ownerEditedFields.add(node);
    invalidateOwnerReview();
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
    if (unconfirmedDispatchResult) {
      // A routine poll must not imply that an uncertain operation was undone.
      setTextIfChanged(statusNode, errorValue || status || "Готово.");
      return;
    }
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
    if (hasFocusedReadback(node)) return;
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
    if (hasFocusedReadback(body)) return;
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
    if (document.activeElement === node) return;
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

  // A rejected durable save must not leave a different selected screen in the
  // focused native selector. A lost response is UNKNOWN, not a rejection: leave
  // that state for explicit reconciliation rather than inventing a rollback.
  // An explicit rejected config change must never remain selected while the
  // focused native control skips ordinary server-state refreshes. A missing
  // bridge response is UNKNOWN, and must not be represented as a rollback.
  function reconcileRejectedControlSelection(node, requestedValue, result, verifiedValue) {
    if (
      result && result.status === "rejected"
      && typeof verifiedValue === "string"
      && node.value === requestedValue
    ) {
      node.value = verifiedValue;
    }
  }

  function reconcileRejectedSurfaceSelection(node, requestedKey, result, state) {
    if (
      result && result.status === "rejected"
      && state && typeof state.surface_key === "string"
      && node.value === requestedKey
    ) {
      node.value = state.surface_key;
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
    let pendingFocusedDefault = false;
    document.querySelectorAll("[data-owner-field]").forEach((node) => {
      const key = node.dataset.ownerField;
      if (
        !Object.prototype.hasOwnProperty.call(defaults, key)
        || ownerEditedFields.has(node)
        || node.value !== ""
      ) {
        return;
      }
      if (document.activeElement === node) {
        pendingFocusedDefault = true;
        return;
      }
      node.value = String(defaults[key]);
    });
    ownerDefaultsApplied = !pendingFocusedDefault;
  }

  function focusResult(result) {
    if (!result || !result.focus_id) return;
    focusOperatorTarget(byId(result.focus_id));
  }

  function invalidateStateProjection() {
    stateProjectionEpoch += 1;
  }

  // The bridge is not trusted to return a complete semantic UI snapshot.
  // Reject malformed state before any control, warning, or bankroll is repainted.
  function validateOperatorSnapshot(value) {
    const record = (item) => item !== null && typeof item === "object" && !Array.isArray(item);
    const invalid = () => { throw new Error("Некоректний стан інтерфейсу."); };
    if (!record(value) || !record(value.owner) || !record(value.manual)) invalid();
    for (const key of ["surfaces", "strategy_choices", "surface_details",
      "live_quotes", "tickets", "evaluation", "log"]) {
      if (value[key] !== undefined && !Array.isArray(value[key])) invalid();
    }
    for (const key of ["lines", "review_lines"]) {
      if (value.owner[key] !== undefined && !Array.isArray(value.owner[key])) invalid();
    }
    if (value.owner.defaults !== undefined && !record(value.owner.defaults)) invalid();
    if (value.owner.can_initialize !== undefined
      && typeof value.owner.can_initialize !== "boolean") invalid();
    if (value.manual.operations !== undefined && !Array.isArray(value.manual.operations)) invalid();
    for (const pair of [[value, "status"], [value, "last_error"],
      [value.owner, "summary"], [value.manual, "status"], [value.manual, "result"]]) {
      const item = pair[0][pair[1]];
      if (item !== undefined && item !== null && typeof item !== "string") invalid();
    }
    if (value.dataset_success_generation !== undefined
        && (!Number.isSafeInteger(value.dataset_success_generation)
            || value.dataset_success_generation < 0)) invalid();
    if (value.agent_learning_busy !== undefined
      && typeof value.agent_learning_busy !== "boolean") invalid();
    if (value.agent_learning_evidence !== undefined && (
      !Array.isArray(value.agent_learning_evidence)
      || value.agent_learning_evidence.length > 10
      || value.agent_learning_evidence.some((item) =>
        typeof item !== "string" || item.length > 600)
    )) invalid();
    if (value.agent_overview !== undefined
      && (!Array.isArray(value.agent_overview)
          || value.agent_overview.length !== 5
          || value.agent_overview.some((item) =>
            typeof item !== "string" || item.length > 600))) invalid();
    if (value.update_diagnostic !== undefined && (
      !record(value.update_diagnostic)
      || typeof value.update_diagnostic.status !== "string"
      || value.update_diagnostic.status.length > 600
      || typeof value.update_diagnostic.busy !== "boolean"
    )) invalid();
    if (value.product_source !== undefined && !record(value.product_source)) invalid();
    if (value.product_source && value.product_source.choices !== undefined
      && !Array.isArray(value.product_source.choices)) invalid();
    if (value.product_runtime !== undefined && !record(value.product_runtime)) invalid();
    // Native selects dereference each option before other panels finish painting.
    // Reject corrupt child collections in advance, not halfway through renderState.
    for (const options of [value.surfaces, value.strategy_choices,
      value.manual.operations, value.product_source && value.product_source.choices]) {
      if (options !== undefined && options !== null
        && (!Array.isArray(options) || options.some((item) => !record(item)))) invalid();
    }
    if (value.product_runtime) {
      if (value.product_runtime.economic_snapshot !== undefined
        && !record(value.product_runtime.economic_snapshot)) invalid();
      for (const key of ["can_start", "can_stop"]) {
        const flag = value.product_runtime[key];
        if (flag !== undefined && typeof flag !== "boolean") invalid();
      }
    }
    return value;
  }

  // A broken state refresh invalidates previously actionable PAPER controls;
  // the separate native emergency STOP and navigation remain available.
  function freezeUnverifiedOperatorControls() {
    latestState = null;
    // An unverified bridge cannot preserve approval of a previous owner preview.
    // Also fence an in-flight preview response from revalidating stale values.
    invalidateOwnerReview();
    for (const id of [101, 102, 103, 104, 105, 106, 107, 108, 109,
      328, 333, 335, "owner-confirm", "product-source-select", "product-source-save",
      "product-runtime-start", "product-runtime-stop",
      "update-diagnostic-button", "agent-learning-refresh"]) {
      setDisabledWithFocusFallback(byId(id), true);
    }
    document.querySelectorAll("[data-owner-field], #327").forEach((node) => {
      setDisabledWithFocusFallback(node, true);
    });
  }

  async function apiState() {
    if (!globalThis.pywebview || !globalThis.pywebview.api) {
      throw new Error("Внутрішній канал застосунку недоступний.");
    }
    const response = await globalThis.pywebview.api.get_state();
    if (!response || response.ok !== true || !response.state) {
      throw new Error("Стан продукту недоступний.");
    }
    return validateOperatorSnapshot(response.state);
  }

  async function dispatch(actionId, payload = {}, options = {}) {
    const useGlobalAnnouncement = options.globalAnnouncement !== false;
    const useResultFocus = options.resultFocus !== false;
    const usePostRefresh = options.postRefresh !== false;
    const focusEpochAtRequest = operatorFocusEpoch;
    const focusedElementAtRequest = document.activeElement;
    invalidateStateProjection();
    try {
      const result = await globalThis.pywebview.api.dispatch({
        request_id: requestId(),
        action_id: actionId,
        payload,
      });
      invalidateStateProjection();
      const rejected = !result || result.status !== "completed";
      if (useGlobalAnnouncement) {
        const message = result && result.message
          ? result.message : (rejected ? "Дію відхилено." : "Готово.");
        if (unconfirmedDispatchResult) {
          // Unrelated successful commands must not erase an earlier UNKNOWN.
          setTextIfChanged(statusNode, message);
        } else {
          announce(message, rejected);
        }
      }
      // Preserve explicit Tab/keyboard navigation, including away-and-back.
      // Announcement and state refresh continue normally without forced focus.
      if (
        useResultFocus
        && operatorFocusEpoch === focusEpochAtRequest
        && document.activeElement === focusedElementAtRequest
      ) {
        focusResult(result);
      }
      if (usePostRefresh) await refreshState();
      return result;
    } catch (_error) {
      invalidateStateProjection();
      if (useGlobalAnnouncement) {
        unconfirmedDispatchResult = true;
        announce(
          "Результат дії НЕ ПІДТВЕРДЖЕНО через збій зв’язку. "
          + "Дія могла вже виконатися. Звірте стан і журнал застосунку; "
          + "не повторюйте команду всліпу.",
          true,
        );
        setHiddenIfChanged(dispatchUncertainAck, false);
        // A delayed failed bridge response must not steal focus after the
        // operator has continued keyboard navigation. Keep the assertive
        // message and UNKNOWN acknowledgment visible in either case.
        if (
          operatorFocusEpoch === focusEpochAtRequest
          && document.activeElement === focusedElementAtRequest
        ) {
          focusOperatorTarget(errorNode);
        }
      }
      return null;
    }
  }

  globalThis.autosportDispatch = dispatch;

  dispatchUncertainAck.addEventListener("click", () => {
    // Acknowledgment dismisses presentation, not the unverified effect.
    unconfirmedDispatchResult = false;
    focusOperatorTarget(statusNode);
    setHiddenIfChanged(dispatchUncertainAck, true);
    clearErrorStatusWithFocusHandoff();
    setTextIfChanged(
      statusNode,
      "Попередня дія залишається непідтвердженою. Перед повтором звірте журнал.",
    );
  });

  function renderState(state) {
    latestState = state;
    confirmValidatedDatasetSelection(state);
    projectLiveState(state.status, state.last_error);
    projectEmergencyStopState(state.emergency_stop);

    setTextIfChanged(byId("workspace-value"), state.workspace || "—");
    setTextIfChanged(byId("active-workspace-value"), state.active_workspace || "—");
    setValueIfChanged(byId(205), state.bank || "");
    setTextIfChanged(byId("dataset-summary"), state.dataset_summary || "");
    renderCanonicalPathUnlessDraft("dataset-path", state.dataset_path || "");
    renderCanonicalPathUnlessDraft("research-plan-path", state.research_plan_path || "");
    setTextIfChanged(byId("research-plan-summary"), state.research_plan_summary || "");

    const strategy = byId(106);
    const previousStrategy = strategy.value;
    setSelectOptions(strategy, state.strategy_choices || []);
    setValueUnlessFocused(strategy, state.strategy_id || previousStrategy);
    renderList(byId("agent-overview-list"), state.agent_overview || []);
    renderList(byId("agent-learning-evidence"), state.agent_learning_evidence || []);
    setTextIfChanged(
      byId("agent-learning-status"),
      state.agent_learning_busy === true
        ? "Підтвердження наукових записів виконується."
        : "Доступні записи агентів наведено нижче; непідтверджене не вважається навчанням.",
    );
    setDisabledWithFocusFallback(
      byId("agent-learning-refresh"), state.agent_learning_busy === true,
    );
    const updateInfo = state.update_diagnostic || {};
    setTextIfChanged(
      byId("update-diagnostic-status"),
      updateInfo.status || "Діагностика недоступна.",
    );
    setDisabledWithFocusFallback(
      byId("update-diagnostic-button"),
      updateInfo.busy === true,
    );

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
    // These backend-dispatch controls were frozen on an invalid state read.
    // Only a newly validated canonical snapshot may make them actionable again.
    setDisabledWithFocusFallback(byId(333), false);
    setDisabledWithFocusFallback(byId(335), false);

    const productSource = state.product_source || {};
    const sourceSelect = byId("product-source-select");
    setSelectOptions(sourceSelect, productSource.choices || []);
    if (!sourceSelectionDraft.dirty) {
      // Empty is meaningful: no source has been configured. A select with
      // no matching empty option then has selectedIndex=-1, not first source.
      setValueUnlessFocused(sourceSelect, productSource.selected_id || "");
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
    setValueIfChanged(
      byId("product-runtime-workspace"),
      productRuntime.workspace || "—",
    );
    setValueIfChanged(
      byId("product-runtime-session-id"),
      productRuntime.session_id || "—",
    );
    setValueIfChanged(
      byId("product-runtime-source-id"),
      productRuntime.source_id || "—",
    );
    setValueIfChanged(
      byId("product-runtime-source-health"),
      productRuntime.source_status || "Стан зовнішнього джерела ще не підтверджено канонічним циклом.",
    );
    setValueIfChanged(
      byId("product-runtime-source-last-success"),
      productRuntime.source_last_success_at || "—",
    );
    const runtimeEconomic = productRuntime.economic_snapshot || {};
    setValueIfChanged(
      byId("product-runtime-economic-cycle"),
      runtimeEconomic.available === true && runtimeEconomic.cycle_index !== null
        ? String(runtimeEconomic.cycle_index)
        : "—",
    );
    setValueIfChanged(
      byId("product-runtime-economic-cycle-last-success"),
      runtimeEconomic.cycle_last_success_at || "—",
    );
    setValueIfChanged(
      byId("product-runtime-paper-book-sha"),
      runtimeEconomic.paper_book_sha256 || "—",
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
            refreshPending = true;
          }
        } catch (_error) {
          if (refreshClosed) break;
          if (requestEpoch === stateProjectionEpoch) {
            freezeUnverifiedOperatorControls();
            if (unconfirmedDispatchResult) {
              setTextIfChanged(statusNode, "Не вдалося оновити стан застосунку.");
            } else {
              announce("Не вдалося оновити стан застосунку.", true);
            }
          } else {
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

  byId(101).addEventListener("click", async () => {
    const editEpoch = pathDraftEdits.get("dataset-path");
    const path = byId("dataset-path").value;
    const result = await dispatch("dataset.select", { path });
    finalizeConfirmedPathSelection("dataset-path", editEpoch, result);
  });
  byId(106).addEventListener("change", async () => {
    const control = byId(106);
    const requested = control.value;
    invalidateOwnerReview();
    const result = await dispatch("strategy.set", { strategy_id: requested });
    reconcileRejectedControlSelection(
      control, requested, result, latestState && latestState.strategy_id,
    );
  });
  byId(107).addEventListener("click", async () => {
    invalidateOwnerReview();
    const editEpoch = pathDraftEdits.get("research-plan-path");
    const path = byId("research-plan-path").value;
    const result = await dispatch("research_plan.select", { path });
    finalizeConfirmedPathSelection("research-plan-path", editEpoch, result);
  });
  byId(103).addEventListener("change", async () => {
    const control = byId(103);
    const requested = control.value;
    const result = await dispatch("replay_speed.set", { speed: Number(requested) });
    reconcileRejectedControlSelection(
      control, requested, result,
      latestState && typeof latestState.replay_speed === "number"
        ? String(latestState.replay_speed) : null,
    );
  });
  byId(104).addEventListener("change", async () => {
    const control = byId(104);
    const requested = control.value;
    const result = await dispatch("live_mode.set", { mode: requested });
    reconcileRejectedControlSelection(
      control, requested, result, latestState && latestState.live_mode,
    );
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
  byId("agent-learning-refresh").addEventListener("click", () => {
    dispatch("agents.status");
  });
  byId("update-diagnostic-button").addEventListener("click", () => {
    dispatch("update.diagnose", {
      request_path: byId("update-request-path").value,
    });
  });
  byId("product-source-save").addEventListener("click", async () => {
    const sourceSelect = byId("product-source-select");
    const requestedId = sourceSelect.value;
    const editEpoch = sourceSelectionDraft.epoch;
    const result = await dispatch("product_source.configure", {
      source_id: requestedId,
    });
    if (sourceSelectionDraft.epoch !== editEpoch) return;
    if (result && result.status === "completed") {
      sourceSelectionDraft.dirty = false;
      // Publish only the backend's newly confirmed selection; do not use the
      // optimistic local choice as a persisted setting.
      await refreshState();
    } else if (result && result.status === "rejected") {
      const verified = latestState && latestState.product_source
        && latestState.product_source.selected_id;
      // A failed state readback is not evidence for a fabricated rollback.
      if (typeof verified === "string") {
        sourceSelectionDraft.dirty = false;
        sourceSelect.value = verified;
      }
    }
    // No reply / UNKNOWN retains the draft and the persistent warning;
    // never present a lost bridge reply as successful configuration.
  });
  byId("product-runtime-start").addEventListener("click", () => {
    dispatch("product_runtime.start");
  });
  byId("product-runtime-stop").addEventListener("click", () => {
    dispatch("product_runtime.stop");
  });

  byId(301).addEventListener("change", async () => {
    const navigation = byId(301);
    const requestedKey = navigation.value;
    const result = await dispatch("surface.select", { surface_key: requestedKey });
    // refreshState preserves a focused selector during ordinary polling, so
    // explicitly undo only a confirmed rejection, not an UNKNOWN result.
    reconcileRejectedSurfaceSelection(navigation, requestedKey, result, latestState);
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
    // Move focus to the disclosure trigger before hiding the subtree that
    // currently contains focus. This avoids a transient body/document focus
    // loss that screen readers can announce as an empty context.
    byId(305).focus();
    ownerPanel.hidden = true;
    byId(305).setAttribute("aria-expanded", "false");
  });
  document.querySelectorAll("[data-owner-field]").forEach((node) => {
    node.addEventListener("input", markOwnerFieldEdited);
    node.addEventListener("change", markOwnerFieldEdited);
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
    byId(330).focus();
    manualPanel.hidden = true;
    byId(330).setAttribute("aria-expanded", "false");
  });
  byId(333).addEventListener("click", () => {
    dispatch("manual.calculate", {
      operation: byId(331).value,
      input: byId(332).value,
    });
  });
  byId(335).addEventListener("click", async () => {
    // Keep the operator's input if the backend rejects the clear, or the
    // bridge outcome is unknown; only a confirmed clear may erase the draft.
    const input = byId(332);
    const pendingDraft = input.value;
    const result = await dispatch("manual.clear");
    // Never erase newer text entered while the asynchronous request ran.
    // dispatch() already handles focus from the server-confirmed response.
    if (result && result.status === "completed" && input.value === pendingDraft) {
      input.value = "";
    }
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