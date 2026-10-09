(() => {
  "use strict";

  const button = document.getElementById("emergency-stop-action");
  const status = document.getElementById("emergency-stop-status");
  if (!(button instanceof HTMLButtonElement) || !(status instanceof HTMLElement)) return;

  let pendingStatus = null;
  let activationInFlight = false;

  function setStatus(message) {
    const value = String(message || "Аварійний STOP: стан недоступний.");
    if (status.textContent !== value) status.textContent = value;
  }

  function focusStatus() {
    status.tabIndex = -1;
    status.focus();
  }

  function beginPendingStatus(message) {
    if (pendingStatus instanceof HTMLElement) pendingStatus.remove();

    // The ordinary 250 ms state poll remains authoritative for durable readback, but
    // it must not replace the operator's in-flight safety feedback with a stale STOP
    // snapshot while the independent backend command is still running. Mute/hide the
    // durable projection only for this transient presentation interval and announce a
    // separate status node that the ordinary poll never owns.
    status.hidden = true;
    status.setAttribute("aria-live", "off");

    const node = document.createElement("p");
    node.id = "emergency-stop-pending-status";
    node.setAttribute("role", "status");
    node.setAttribute("aria-live", "assertive");
    node.setAttribute("aria-atomic", "true");
    node.tabIndex = -1;
    node.textContent = String(message);
    status.insertAdjacentElement("afterend", node);
    pendingStatus = node;
    node.focus();
  }

  function focusPendingStatus() {
    if (pendingStatus instanceof HTMLElement) {
      pendingStatus.focus();
      return;
    }
    focusStatus();
  }

  function finishPendingStatus(message) {
    // Project the immediate backend result through the canonical status node before
    // re-enabling ordinary durable-state convergence. The activation path then gives
    // the keyboard/screen-reader operator one bounded result focus handoff; subsequent
    // polls may replace it only with the actual durable STOP state.
    setStatus(message);
    status.hidden = false;
    status.setAttribute("aria-live", "assertive");
    status.setAttribute("aria-atomic", "true");
    if (pendingStatus instanceof HTMLElement) pendingStatus.remove();
    pendingStatus = null;
  }

  async function activateEmergencyStop() {
    const dispatch = globalThis.autosportDispatch;
    if (typeof dispatch !== "function") {
      setStatus("АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. Канал застосунку недоступний.");
      focusStatus();
      return;
    }

    // Never disable the safety control, but also never emit two overlapping backend
    // commands from rapid Enter/Space/click activation. A repeated activation while
    // the command is in flight simply returns the operator to the existing pending
    // announcement. This prevents out-of-order completions from projecting stale
    // success/failure text while preserving immediate keyboard reachability.
    if (activationInFlight) {
      focusPendingStatus();
      return;
    }

    // Emergency backend dispatch is an independent safety lane. Give a
    // blind/keyboard operator immediate, explicitly non-final feedback before
    // awaiting the backend result. The shared dispatcher skips only its synchronous
    // post-command state refresh for this path; periodic polling remains canonical
    // durable-state convergence. While the command is in flight, the pending live
    // region prevents a stale ordinary poll from audibly/visually replacing this
    // safety feedback.
    beginPendingStatus(
      "Аварійний STOP: запит передано. "
      + "Очікується підтвердження стійкого журналу заборони.",
    );
    activationInFlight = true;

    let resultMessage;
    try {
      const result = await dispatch(
        "emergency_stop.activate",
        {},
        {
          globalAnnouncement: false,
          resultFocus: false,
          postRefresh: false,
        },
      );
      if (result === null) {
        resultMessage = (
          "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. "
          + "Не вважайте нові виконання заблокованими без підтвердження; перевірте стійкий журнал STOP."
        );
      } else {
        resultMessage = (
          result.message
          || (
            result.status === "completed"
              ? "Аварійний STOP: команда завершена. Перевірте стійкий стан."
              : "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. Перевірте журнал STOP."
          )
        );
      }
    } catch (_error) {
      resultMessage = (
        "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. "
        + "Не вважайте нові виконання заблокованими без підтвердження; перевірте стійкий журнал STOP."
      );
    } finally {
      activationInFlight = false;
    }
    // This is immediate backend-result confirmation. It is not a second state
    // authority: the existing poll loop will converge this readback to durable state.
    finishPendingStatus(resultMessage);
    focusStatus();
  }

  button.addEventListener("click", activateEmergencyStop);
})();