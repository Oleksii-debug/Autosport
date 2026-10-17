/* AS-08 source-only UI adapter for the canonical durable operator-status broker.
   Requires a trusted, redacted projection and a real backend acknowledgement.
   Not wired into the AS-02 host; cannot imply deployment or acceptance. */
(function (global) {
  "use strict";
  const mounts = new WeakMap();
  let headingSerial = 0;
  const EVENT_ID = /^[0-9a-f]{64}$/;
  const SESSION_ID = /^[A-Za-z0-9._:-]{1,128}$/;

  function put(node, value) {
    const text = String(value);
    if (node.textContent !== text) node.textContent = text;
  }

  function validated(snapshot) {
    // `complete` is an authoritative broker-side assertion: a truncated page
    // must never masquerade as a quiet/pending-complete journal.
    if (!snapshot || snapshot.ready !== true || snapshot.redacted !== true ||
        snapshot.complete !== true ||
        typeof snapshot.session_id !== "string" || !SESSION_ID.test(snapshot.session_id) ||
        !Array.isArray(snapshot.pending) || snapshot.pending.length > 1000) return null;
    const ids = new Set();
    const sequences = new Set();
    const events = [];
    for (const e of snapshot.pending) {
      if (!e || typeof e !== "object" || !Number.isSafeInteger(e.sequence) || e.sequence < 1 ||
          typeof e.event_id !== "string" || !EVENT_ID.test(e.event_id) ||
          (e.priority !== "POLITE" && e.priority !== "ASSERTIVE") ||
          typeof e.message !== "string" || !e.message || e.message.trim() !== e.message ||
          e.message.length > 4096 || e.acknowledged_at != null || e.superseded_by != null ||
          ids.has(e.event_id) || sequences.has(e.sequence)) return null;
      ids.add(e.event_id);
      sequences.add(e.sequence);
      events.push(e);
    }
    events.sort((a, b) => a.sequence - b.sequence);
    return { session_id: snapshot.session_id, events };
  }

  function mount({ container, acknowledge }) {
    if (!container || !container.ownerDocument || typeof acknowledge !== "function") {
      throw new TypeError("Reader needs a DOM container and real acknowledgement adapter");
    }
    if (mounts.has(container)) return mounts.get(container);
    const doc = container.ownerDocument;
    const heading = doc.createElement("h2");
    const availability = doc.createElement("p");
    const list = doc.createElement("ol");
    const urgent = doc.createElement("p");
    const actionStatus = doc.createElement("p");
    // Multiple reader panes in one document must not share an accessible label ID.
    heading.id = "autosport-notification-heading-" + (++headingSerial);
    heading.tabIndex = -1;
    put(heading, "Сповіщення та журнал повідомлень");
    container.setAttribute("aria-labelledby", heading.id);
    list.setAttribute("aria-label", "Повідомлення за порядком подій");
    urgent.setAttribute("role", "alert");
    urgent.setAttribute("aria-live", "assertive");
    urgent.setAttribute("aria-atomic", "true");
    actionStatus.setAttribute("role", "status");
    actionStatus.setAttribute("aria-live", "polite");
    for (const n of [heading, availability, list, urgent, actionStatus]) container.appendChild(n);

    let session = null;
    let online = false;
    let current = new Map();
    let inflight = new Set();
    let awaitingReadback = new Set();
    let generation = 0; // invalidates acknowledgements belonging to prior sessions
    const elements = new Map();
    const announced = new Set();
    let activeUrgentId = null;
    function availabilityMessage(message, enabled) {
      online = enabled;
      put(availability, message);
      for (const [id, entry] of elements) {
        const disable = !enabled || inflight.has(id) || awaitingReadback.has(id);
        if (disable && doc.activeElement === entry.button) heading.focus();
        entry.button.disabled = disable;
      }
    }
    function entryFor(event) {
      const li = doc.createElement("li");
      const title = doc.createElement("h3");
      const body = doc.createElement("pre");
      const origin = doc.createElement("p");
      const button = doc.createElement("button");
      body.tabIndex = 0;
      body.style.whiteSpace = "pre-wrap";
      body.style.overflowWrap = "anywhere";
      button.type = "button";
      button.addEventListener("click", () => { void acknowledgeEvent(event.event_id); });
      for (const item of [title, body, origin, button]) li.appendChild(item);
      return { li, title, body, origin, button };
    }
    function updateEntry(entry, event) {
      put(entry.title, (event.priority === "ASSERTIVE" ? "Важливе" : "Інформація") +
        " — повідомлення №" + event.sequence);
      put(entry.body, event.message); // never innerHTML: literal, selectable diagnostics
      put(entry.origin, "Порядковий номер " + event.sequence + "; ідентифікатор події " + event.event_id);
      put(entry.button, "Позначити прочитаним повідомлення №" + event.sequence);
      entry.button.disabled = !online || inflight.has(event.event_id) ||
        awaitingReadback.has(event.event_id);
    }
    function render(snapshot) {
      if (!snapshot || snapshot.ready !== true) {
        availabilityMessage("Журнал недоступний. Раніше показані дані можуть бути застарілими; перевірте підключення.", false);
        return false;
      }
      const data = validated(snapshot);
      if (!data) {
        availabilityMessage("Журнал повернув некоректні або неочищені дані. Оновлення відхилено; перегляньте діагностику.", false);
        return false;
      }
      if (session !== null && session !== data.session_id) {
        availabilityMessage("Стан іншої сесії відхилено. Потрібне підтвердження перезапуску.", false);
        return false;
      }
      if (session === null) session = data.session_id;
      // A verified refresh cannot resurrect a source record under a different identity.
      // A durable event identity cannot acquire a different sequence or content.
      for (const event of data.events) {
        const old = current.get(event.event_id);
        if (old && (old.sequence !== event.sequence || old.priority !== event.priority ||
            old.message !== event.message)) {
          availabilityMessage("Джерело змінило незмінний запис події. Оновлення відхилено.", false);
          return false;
        }
      }
      const next = new Map(data.events.map(e => [e.event_id, e]));
      const focused = doc.activeElement;
      for (const [id, entry] of elements) {
        if (next.has(id)) continue;
        if (entry.li.contains(focused)) heading.focus();
        list.removeChild(entry.li);
        elements.delete(id);
        awaitingReadback.delete(id);
      }
      online = true;
      for (const [position, event] of data.events.entries()) {
        let entry = elements.get(event.event_id);
        if (!entry) {
          entry = entryFor(event);
          elements.set(event.event_id, entry);
          // A delayed older event still appears in canonical sequence order.
          const before = list.children[position] || null;
          if (before) list.insertBefore(entry.li, before);
          else list.appendChild(entry.li);
        }
        updateEntry(entry, event);
      }
      current = next;
      availabilityMessage(data.events.length === 0 ?
        "Нових сповіщень немає. Канонічний журнал доступний." :
        "Очікують прочитання: " + data.events.length + ".", true);
      const urgentNew = data.events.filter(e => e.priority === "ASSERTIVE" && !announced.has(e.event_id));
      for (const event of data.events) announced.add(event.event_id);
      while (announced.size > 4096) announced.delete(announced.values().next().value);
      if (!urgentNew.length && activeUrgentId && !next.has(activeUrgentId)) {
        put(urgent, "");
        activeUrgentId = null;
      }
      if (urgentNew.length) {
        const latest = urgentNew[urgentNew.length - 1];
        activeUrgentId = latest.event_id;
        put(urgent, (urgentNew.length > 1 ? "Нових важливих повідомлень: " + urgentNew.length + ". Останнє: " :
          "Важливе повідомлення: ") + latest.message);
      }
      return true;
    }
    async function acknowledgeEvent(eventId) {
      if (!online || !current.has(eventId) || inflight.has(eventId) ||
          awaitingReadback.has(eventId)) return false;
      const requestSession = session;
      const requestGeneration = generation;
      inflight.add(eventId);
      const existing = elements.get(eventId);
      if (existing) existing.button.disabled = true;
      put(actionStatus, "Надсилається підтвердження прочитання…");
      try {
        const receipt = await acknowledge({ event_id: eventId, session_id: requestSession });
        if (requestSession !== session || requestGeneration !== generation ||
            !current.has(eventId)) return false;
        if (!receipt || receipt.status !== "completed" || receipt.event_id !== eventId ||
            receipt.session_id !== requestSession) {
          put(actionStatus, "Прочитання не підтверджено. Повідомлення збережено. Звірте журнал перед повтором.");
          return false;
        }
        awaitingReadback.add(eventId);
        put(actionStatus, "Підтвердження прийнято. Очікується оновлення авторитетного журналу.");
        return true; // never hide an event without the next trusted source snapshot
      } catch (_error) {
        if (requestSession === session && requestGeneration === generation) put(actionStatus,
          "Зв’язок перервано. Підтвердження невідоме; звірте журнал перед повтором.");
        return false;
      } finally {
        // An old asynchronous completion must never change a new session's UI.
        if (requestGeneration === generation) {
          inflight.delete(eventId);
          const entry = elements.get(eventId);
          if (entry) entry.button.disabled = !online || awaitingReadback.has(eventId);
        }
      }
    }
    function resetForVerifiedRestart(newSession) {
      if (typeof newSession !== "string" || !SESSION_ID.test(newSession)) return false;
      const focused = doc.activeElement;
      for (const entry of elements.values()) {
        if (entry.li.contains(focused)) heading.focus();
        list.removeChild(entry.li);
      }
      generation += 1;
      elements.clear();
      current = new Map();
      inflight = new Set();
      awaitingReadback = new Set();
      announced.clear();
      activeUrgentId = null;
      session = newSession;
      online = false;
      put(urgent, "");
      put(actionStatus, "");
      availabilityMessage("Перезапуск підтверджено; очікується стан нового журналу.", false);
      return true;
    }
    const api = Object.freeze({ render, acknowledgeEvent, resetForVerifiedRestart,
      state: () => Object.freeze({ session_id: session, available: online,
        pending_ids: Object.freeze(Array.from(current.keys())) }) });
    mounts.set(container, api);
    availabilityMessage("Журнал ще не під’єднано до канонічного джерела.", false);
    return api;
  }
  global.AutosportNotificationReader = Object.freeze({ mount });
})(globalThis);
