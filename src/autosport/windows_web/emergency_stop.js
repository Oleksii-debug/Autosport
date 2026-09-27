(() => {
  "use strict";

  const button = document.getElementById("emergency-stop-action");
  const status = document.getElementById("emergency-stop-status");
  if (!(button instanceof HTMLButtonElement) || !(status instanceof HTMLElement)) return;

  function setStatus(message) {
    const value = String(message || "Аварійний STOP: стан недоступний.");
    if (status.textContent !== value) status.textContent = value;
  }

  function focusStatus() {
    status.tabIndex = -1;
    status.focus();
  }

  async function activateEmergencyStop() {
    const dispatch = globalThis.autosportDispatch;
    if (typeof dispatch !== "function") {
      setStatus("АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. Канал застосунку недоступний.");
      focusStatus();
      return;
    }

    // Emergency backend dispatch is an independent safety lane. Give a
    // blind/keyboard operator immediate, explicitly non-final feedback before
    // awaiting the backend result. The shared dispatcher skips only its synchronous
    // post-command state refresh for this path; periodic polling remains canonical
    // durable-state convergence.
    setStatus(
      "Аварійний STOP: запит передано. "
      + "Очікується підтвердження стійкого журналу заборони.",
    );
    focusStatus();

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
        setStatus(
          "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. "
          + "Нові виконання мають залишатися заблокованими; перевірте журнал STOP.",
        );
      } else {
        setStatus(
          result.message
          || (
            result.status === "completed"
              ? "Аварійний STOP: команда завершена. Перевірте стійкий стан."
              : "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. Перевірте журнал STOP."
          ),
        );
      }
    } catch (_error) {
      setStatus(
        "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. "
        + "Нові виконання мають залишатися заблокованими; перевірте журнал STOP.",
      );
    }
    // This is immediate backend-result confirmation. It is not a second state
    // authority: the existing poll loop will converge this readback to durable state.
    focusStatus();
  }

  button.addEventListener("click", activateEmergencyStop);
})();