/* Run from repository root: node tests/test_windows_webview_notification_reader.js */
"use strict";
require("../src/autosport/windows_web/notification_reader.js");
const { mount } = globalThis.AutosportNotificationReader;
const id = char => char.repeat(64);
const event = (seq, char, priority = "ASSERTIVE", message = "Помилка джерела.") =>
  ({ sequence: seq, event_id: id(char), priority, message, acknowledged_at: null, superseded_by: null });
const data = (session, pending, redacted = true) =>
  ({ ready: true, redacted, complete: true, session_id: session, pending });
let passed = 0;
function ok(condition, why) { if (!condition) throw new Error("FAIL: " + why); passed++; }
function dom() {
  const document = { activeElement: null };
  function make(tag) {
    return {
      tagName: tag.toUpperCase(), ownerDocument: document, parentNode: null,
      children: [], attributes: {}, style: {}, textContent: "", disabled: false,
      listeners: {}, setAttribute(key,value) { this.attributes[key] = value; },
      addEventListener(key, listener) { this.listeners[key] = listener; },
      appendChild(child) {
        if (child.parentNode) child.parentNode.removeChild(child);
        this.children.push(child); child.parentNode = this; return child;
      },
      insertBefore(child, before) {
        if (child.parentNode) child.parentNode.removeChild(child);
        const index = before == null ? this.children.length : this.children.indexOf(before);
        ok(index >= 0, "insertion anchor exists");
        this.children.splice(index, 0, child); child.parentNode = this; return child;
      },
      removeChild(child) {
        const n = this.children.indexOf(child);
        ok(n >= 0, "removed node is a child");
        this.children.splice(n, 1); child.parentNode = null; return child;
      },
      contains(child) {
        while (child) { if (child === this) return true; child = child.parentNode; }
        return false;
      },
      focus() { document.activeElement = this; },
    };
  }
  document.createElement = make;
  const container = make("section");
  return { document, container };
}
function newPane(ack = async () => null) {
  const { document, container } = dom();
  return { pane: mount({container, acknowledge: ack}), document, container };
}
async function run() {
  {
    const {pane,container} = newPane();
    ok(pane.render(null) === false && container.children[1].textContent.includes("недоступний"),
      "unavailable is not quiet");
    ok(pane.render(data("session-a", [])) && container.children[1].textContent.includes("немає"),
      "verified empty state quiet");
  }
  {
    const {pane,document,container} = newPane();
    const long = "<img src=x onerror=alert(1)>" + "Ж".repeat(4000);
    const first = event(1,"a","ASSERTIVE",long);
    ok(pane.render(data("session-a",[first])) &&
      container.children[2].children[0].children[1].textContent === long,
      "long selectable message is literal text, not HTML");
    const firstAlert = container.children[3].textContent;
    ok(pane.render(data("session-a",[first])) && container.children[3].textContent === firstAlert,
      "repeated event is not announced again");
    const second = event(2,"b","POLITE","Джерело відновлено.");
    pane.render(data("session-a",[first,second]));
    ok(container.children[2].children.length === 2 &&
      container.children[2].children[0].children[0].textContent.includes("№1"),
      "source chronology preserved");
    container.children[2].children[0].children[1].focus();
    pane.render(data("session-a",[second]));
    ok(container.children[3].textContent === "", "cleared urgent event does not remain active");
    ok(document.activeElement === container.children[0] && container.children[2].children.length === 1,
      "replacement clears focused item with deterministic heading fallback");
    ok(!pane.render(data("session-other",[event(3,"c")])) && pane.state().session_id === "session-a",
      "foreign session rejected");
    ok(pane.resetForVerifiedRestart("session-other") && pane.render(data("session-other",[event(3,"c")])),
      "verified restart accepted");
    const delayed = event(2,"d","POLITE","Дійшла затримана подія.");
    pane.render(data("session-other",[event(3,"c"),delayed]));
    ok(container.children[2].children[0].children[0].textContent.includes("№2"),
      "late older pending notification inserted before newer event");
  }
  {
    const requests = [];
    const {pane} = newPane(async command => {
      requests.push(command);
      return {status:"completed", ...command};
    });
    const item = event(1,"d");
    pane.render(data("session-ack",[item]));
    ok(await pane.acknowledgeEvent(item.event_id) && requests.length === 1 &&
      requests[0].session_id === "session-ack", "exact acknowledged event/session adapter");
    ok(pane.state().pending_ids.length === 1, "no optimistic success deletion");
    ok(!(await pane.acknowledgeEvent(item.event_id)) && requests.length === 1,
      "acknowledged receipt suppresses duplicate send until authoritative readback");
    pane.render(data("session-ack",[]));
    ok(pane.state().pending_ids.length === 0, "source-authoritative acknowledgement clears");
  }
  {
    const {pane} = newPane(async () => ({status:"completed",event_id:id("e"),session_id:"foreign"}));
    pane.render(data("session-ack",[event(1,"e")]));
    ok(!(await pane.acknowledgeEvent(id("e"))) && pane.state().pending_ids.length === 1,
      "foreign acknowledgement cannot remove event");
    ok(!pane.render(data("session-ack",[event(1,"e"),event(2,"e")])),
      "duplicate source ID rejected");
    ok(!pane.render(data("session-ack",[event(2,"e")])),
      "event content cannot change under stable event ID");
    ok(!pane.render(data("session-ack",[event(1,"e")],false)) && !pane.state().available,
      "uncertified redaction fails closed");
  }
  {
    let release;
    const {pane} = newPane(() => new Promise(resolve => { release = resolve; }));
    pane.render(data("old-session",[event(1,"f")]));
    const oldPending = pane.acknowledgeEvent(id("f"));
    pane.resetForVerifiedRestart("new-session");
    pane.render(data("new-session",[event(1,"a")]));
    release({status:"completed",event_id:id("f"),session_id:"old-session"});
    ok((await oldPending) === false && pane.state().session_id === "new-session",
      "interrupted previous-session ACK cannot cross restart");
  }
  {
    const {pane,container} = newPane();
    const incomplete = { ...data("session-page", []), complete: false };
    ok(!pane.render(incomplete) && !pane.state().available &&
       !container.children[1].textContent.includes("немає"),
       "truncated broker page cannot masquerade as a quiet empty journal");
  }
  {
    const {pane,document,container} = newPane();
    const item = event(1,"e");
    pane.render(data("session-focus",[item]));
    const button = container.children[2].children[0].children[3];
    button.focus();
    ok(!pane.render(null) && document.activeElement === container.children[0] && button.disabled,
      "disconnect moves focus from newly disabled ACK to stable heading");
  }
  {
    const {pane:p1, container:c1} = newPane();
    const {pane:p2, container:c2} = newPane();
    ok(c1.children[0].id !== c2.children[0].id &&
       c1.attributes["aria-labelledby"] === c1.children[0].id &&
       c2.attributes["aria-labelledby"] === c2.children[0].id,
       "parallel readers get nonconflicting accessible heading IDs");
  }
  {
    let release;
    const {pane,container} = newPane(() => new Promise(resolve => { release = resolve; }));
    pane.render(data("old",[event(1,"f")]));
    const pending = pane.acknowledgeEvent(id("f"));
    pane.resetForVerifiedRestart("new");
    pane.render(data("new",[event(1,"f")]));
    release({status:"completed", event_id:id("f"), session_id:"old"});
    ok(!(await pending) && !container.children[2].children[0].children[3].disabled &&
       container.children[4].textContent === "",
       "late old-session ACK cannot change reused event ID or action status");
  }
  {
    const {pane} = newPane(async () => { throw new Error("SECRET=private"); });
    pane.render(data("timeout-session",[event(1,"b")]));
    ok(!(await pane.acknowledgeEvent(id("b"))) && pane.state().pending_ids.length === 1,
      "failed or cancelled ACK retains event without leaking exception");
  }
  console.log("PASS " + passed + " source-executing notification reader checks");
}
run().catch(error => { console.error(error); process.exitCode = 1; });
