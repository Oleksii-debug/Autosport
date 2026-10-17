"""Executable regression: delayed WebView action must respect keyboard navigation.

Runs canonical src/autosport/windows_web/app.js with a deferred bridge.
Machine-only DOM witness; never presented as external UIA or NVDA acceptance.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "src/autosport/windows_web/app.js"

JS = r"""
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');

function environment() {
    const all = new Map();
    const events = new Map();
    let respond;
    const pending = new Promise(resolve => { respond = resolve; });
    let doc;
    class HTMLElement {
        constructor(id) {
            this.id = id;
            this.tagName = 'BUTTON';
            this.value = '';
            this.textContent = '';
            this.hidden = false;
            this.disabled = false;
            this.tabIndex = 0;
            this.events = new Map();
        }
        focus() {
            const changed = doc.activeElement !== this;
            doc.activeElement = this;
            if (changed) for (const cb of events.get('focusin') || [])
                cb({target: this, type: 'focusin'});
        }
        matches(s) { return s === ':disabled' && this.disabled; }
        closest() { return null; }
        querySelector() { return null; }
        contains(node) { return node === this; }
        addEventListener(name, cb) { this.events.set(name, cb); }
        setAttribute() {}
        getAttribute() { return null; }
    }
    function byId(id) {
        id = String(id);
        if (!all.has(id)) all.set(id, new HTMLElement(id));
        return all.get(id);
    }
    doc = {
        activeElement: null,
        getElementById: byId,
        querySelectorAll: () => [],
        addEventListener(name, cb) {
            if (!events.has(name)) events.set(name, []);
            events.get(name).push(cb);
        }
    };
    doc.body = byId('body');
    const context = {
        document: doc,
        HTMLElement,
        window: {
            addEventListener: () => {},
            setInterval: () => 1,
            clearInterval: () => {},
        },
        console, setTimeout, clearTimeout,
        crypto: {randomUUID: () => 'test-only-request-id'},
        pywebview: {
            api: {
                dispatch: () => pending,
                get_state: async () => ({ok: true, state: {}}),
            },
        },
    };
    context.globalThis = context;
    vm.createContext(context);
    vm.runInContext(source, context, {filename: process.argv[1], timeout: 2000});
    assert.equal(typeof context.autosportDispatch, 'function');
    return {byId, respond, doc, dispatch: context.autosportDispatch};
}

async function scenario(navigate) {
    const ui = environment();
    const invoked = ui.byId(101);
    const destination = ui.byId(106);
    invoked.focus();
    const running = ui.dispatch('dataset.select', {path: 'offline-test-only'}, {postRefresh: false});
    if (navigate === 'away') destination.focus();
    if (navigate === 'roundtrip') {
        destination.focus();
        invoked.focus();
    }
    ui.respond({status: 'completed', message: 'Completed', focus_id: '302'});
    await running;
    return ui.doc.activeElement.id;
}

(async () => {
    const actual = [
        await scenario('none'),
        await scenario('away'),
        await scenario('roundtrip'),
    ];
    assert.deepEqual(
        actual,
        ['302', '106', '101'],
        'Late async action response must not steal keyboard focus after Tab navigation'
    );
    process.stdout.write(JSON.stringify({witness: 'PASS', actual}) + '\n');
})().catch(e => {console.error(e.stack || e); process.exitCode = 1;});
"""


def test_delayed_response_preserves_keyboard_focus() -> None:
    assert APP.is_file(), f"Canonical app.js is absent: {APP}"
    node = shutil.which("node")
    assert node, "Node is required for the executable JS focus gate (not SKIP/PASS)"
    result = subprocess.run(
        [node, "-e", JS, str(APP)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, (
        "Real app.js delayed dispatch focus race FAILED. "
        f"stdout={result.stdout[-1500:]!r}; stderr={result.stderr[-2500:]!r}"
    )
    assert '"witness":"PASS"' in result.stdout
