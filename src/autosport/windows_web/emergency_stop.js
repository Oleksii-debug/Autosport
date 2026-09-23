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

    try {
      const result = await dispatch(
        "emergency_stop.activate",
        {},
        { globalAnnouncement: false },
      );
      if (result === null) {
        setStatus(
          "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. "
          + "Нові виконання мають залишатися заблокованими; перевірте журнал STOP.",
        );
      } else if (result.status !== "completed") {
        setStatus(
          result.message
          || "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. Перевірте журнал STOP.",
        );
      }
    } catch (_error) {
      setStatus(
        "АВАРІЙНИЙ STOP НЕ ПІДТВЕРДЖЕНО. "
        + "Нові виконання мають залишатися заблокованими; перевірте журнал STOP.",
      );
    }
    // The shared dispatcher has already forced one causally post-command state
    // refresh. Focus only after that refresh; this asset is not a second state writer.
    focusStatus();
  }

  button.addEventListener("click", activateEmergencyStop);
})();
