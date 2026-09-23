(() => {
  "use strict";

  const button = document.getElementById("emergency-stop-action");
  const status = document.getElementById("emergency-stop-status");
  if (!(button instanceof HTMLButtonElement) || !(status instanceof HTMLElement)) return;

  function requestId() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
      return globalThis.crypto.randomUUID();
    }
    return "autosport-emergency-stop-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }

  function setStatus(message) {
    const value = String(message || "Аварійний STOP: стан недоступний.");
    if (status.textContent !== value) status.textContent = value;
  }

  function focusStatus() {
    status.tabIndex = -1;
    status.focus();
  }

  async function readEmergencyState() {
    if (!globalThis.pywebview || !globalThis.pywebview.api) return;
    try {
      const response = await globalThis.pywebview.api.get_state();
      const emergency = response && response.ok === true && response.state
        ? response.state.emergency_stop
        : null;
      if (emergency && emergency.status) setStatus(emergency.status);
    } catch (_error) {
      // The main shell owns general bridge-error presentation. Keep this safety
      // control available so the operator can retry without a false success state.
    }
  }

  async function activateEmergencyStop() {
    if (!globalThis.pywebview || !globalThis.pywebview.api) {
      setStatus("АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. Канал застосунку недоступний.");
      focusStatus();
      return;
    }

    try {
      const result = await globalThis.pywebview.api.dispatch({
        request_id: requestId(),
        action_id: "emergency_stop.activate",
        payload: {},
      });
      const confirmed = result && result.status === "completed";
      setStatus(
        result && result.message
          ? result.message
          : (
              confirmed
                ? "Аварійний STOP підтверджено."
                : "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. Перевірте журнал STOP."
            ),
      );
      focusStatus();
    } catch (_error) {
      setStatus(
        "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. "
        + "Нові виконання мають залишатися заблокованими; перевірте журнал STOP.",
      );
      focusStatus();
    }
  }

  button.addEventListener("click", activateEmergencyStop);
  window.addEventListener("pywebviewready", readEmergencyState, { once: true });
})();
