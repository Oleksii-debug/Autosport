"""Plan 6 §3А: one canonical, keyboard-reachable PAPER operator journey.

This source regression does not claim Windows execution, NVDA, live betting,
release acceptance, or that the whole §3А acceptance matrix is complete.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import json
import re


ROOT = Path(__file__).resolve().parents[1]
PRODUCT = ROOT / "src" / "autosport"
WEB = PRODUCT / "windows_web"

# Each exposed bridge command has one actual operator control, not a hidden
# JavaScript-only escape hatch or a second disconnected product controller.
JOURNEY_CONTROLS = {
    "dataset.select": "101",
    "strategy.set": "106",
    "research_plan.select": "107",
    "replay_speed.set": "103",
    "live_mode.set": "104",
    "replay.run": "102",
    "live.refresh": "105",
    "recovery.run": "108",
    "evidence.export": "109",
    "agents.status": "agent-learning-refresh",
    "update.diagnose": "update-diagnostic-button",
    "product_source.configure": "product-source-save",
    "product_runtime.start": "product-runtime-start",
    "product_runtime.stop": "product-runtime-stop",
    "surface.select": "301",
    "owner.preview": "328",
    "owner.initialize": "owner-confirm",
    "manual.calculate": "333",
    "manual.clear": "335",
}


class _OperatorMarkup(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.duplicates: list[str] = []
        self.explicit_labels: set[str] = set()
        self.forms: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        identifier = attributes.get("id")
        if identifier:
            if identifier in self.ids:
                self.duplicates.append(identifier)
            self.ids.add(identifier)
        if tag == "label" and attributes.get("for"):
            self.explicit_labels.add(str(attributes["for"]))
        if tag in {"input", "select", "textarea"} and attributes.get("type") != "hidden":
            self.forms.append((tag, attributes))


def _sources() -> tuple[str, str, str, _OperatorMarkup]:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    backend = (PRODUCT / "windows_webview_shell.py").read_text(encoding="utf-8")
    markup = _OperatorMarkup()
    markup.feed(html)
    markup.close()
    return html, js, backend, markup


def test_every_webview_bridge_action_is_wired_to_the_real_product_controller() -> None:
    _, js, backend, _ = _sources()
    js_actions = set(re.findall(r'dispatch\("([a-z_]+\.[a-z_]+)"', js))
    backend_actions = set(re.findall(
        r'"([a-z_]+\.[a-z_]+)"\s*:\s*self\._action_', backend,
    ))
    assert js_actions == backend_actions == set(JOURNEY_CONTROLS)
    # Keep bookmaker account/real-money writes unavailable in the PAPER shell.
    assert not any("bet" in action or "stake" in action for action in js_actions)


def test_all_exposed_actions_have_present_interactive_controls() -> None:
    _, js, _, markup = _sources()
    assert not markup.duplicates
    for action, identifier in JOURNEY_CONTROLS.items():
        assert identifier in markup.ids, (action, identifier)
        token = identifier if identifier.isdecimal() else json.dumps(identifier)
        assert f"byId({token}).addEventListener(" in js, (action, identifier)
        assert f'dispatch("{action}"' in js, action


def test_webview_rendered_control_ids_and_form_names_are_source_complete() -> None:
    _, js, _, markup = _sources()
    referenced_ids = {
        quoted or number
        for quoted, number in re.findall(
            r"""\bbyId\((?:['"]([^'"]+)['"]|([0-9]+))\)""", js,
        )
    }
    assert referenced_ids <= markup.ids, sorted(referenced_ids - markup.ids)
    assert len(markup.forms) >= 40  # Protect complete operator form breadth.
    for tag, attributes in markup.forms:
        identifier = attributes.get("id")
        assert identifier, (tag, attributes)
        has_accessible_name = (
            identifier in markup.explicit_labels
            or bool(attributes.get("aria-label"))
            or bool(attributes.get("aria-labelledby"))
        )
        assert has_accessible_name, (tag, identifier)


def test_stop_fallback_and_unknown_effect_are_not_fake_success() -> None:
    html, js, backend, markup = _sources()
    entry = (PRODUCT / "windows_entry.py").read_text(encoding="utf-8")
    safety_script = (WEB / "emergency_stop.js").read_text(encoding="utf-8")
    assert "--native-emergency-stop" in entry
    assert "windows_emergency_stop" in entry
    assert "emergency-stop-action" in markup.ids
    assert 'getElementById("emergency-stop-action")' in safety_script
    assert "emergency-stop-status" in markup.ids
    assert "dispatch-uncertain-ack" in markup.ids
    assert "unconfirmedDispatchResult = true" in js
    assert "не повторюйте команду всліпу" in js
    assert '"update.diagnose": self._action_update_diagnostic' in backend
    assert "Реальні грошові ставки вимкнено" in html
