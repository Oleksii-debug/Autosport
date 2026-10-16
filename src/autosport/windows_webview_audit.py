from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .integrity import atomic_write_json
from .windows_webview_shell import web_shell_index_path


_REQUIRED_CONTROLS = {
    "101": "button",
    "102": "button",
    "103": "select",
    "104": "select",
    "105": "button",
    "106": "select",
    "107": "button",
    "108": "button",
    "109": "button",
    "201": "table",
    "202": "textarea",
    "203": "ul",
    "204": "ul",
    "205": "input",
    "301": "select",
    "302": "input",
    "303": "button",
    "304": "ul",
    "305": "button",
    "306": "input",
    "307": "ul",
    "309": "input",
    "310": "input",
    "311": "input",
    "312": "input",
    "313": "input",
    "314": "input",
    "315": "input",
    "316": "input",
    "317": "input",
    "318": "input",
    "319": "input",
    "320": "input",
    "321": "input",
    "322": "input",
    "323": "input",
    "324": "input",
    "325": "input",
    "326": "input",
    "327": "input",
    "328": "button",
    "329": "button",
    "330": "button",
    "331": "select",
    "332": "textarea",
    "333": "button",
    "334": "textarea",
    "335": "button",
    "336": "button",
    "product-source-select": "select",
    "product-source-save": "button",
    "product-source-status": "input",
    "product-runtime-start": "button",
    "product-runtime-stop": "button",
    "product-runtime-status": "input",
    "product-runtime-workspace": "input",
    "product-runtime-session-id": "input",
    "product-runtime-source-id": "input",
    "product-runtime-source-health": "input",
    "product-runtime-source-last-success": "input",
    "product-runtime-economic-cycle": "input",
    "product-runtime-economic-cycle-last-success": "input",
    "product-runtime-paper-book-sha": "input",
    "emergency-stop-action": "button",
}
_READONLY_CONTROLS = {
    "202",
    "205",
    "302",
    "306",
    "334",
    "product-source-status",
    "product-runtime-status",
    "product-runtime-workspace",
    "product-runtime-session-id",
    "product-runtime-source-id",
    "product-runtime-source-health",
    "product-runtime-source-last-success",
    "product-runtime-economic-cycle",
    "product-runtime-economic-cycle-last-success",
    "product-runtime-paper-book-sha",
}
_LIST_CONTROLS = {"203", "204", "304", "307"}
_TABLE_CONTROLS = {"201"}
_DYNAMICALLY_DISABLED_CONTROLS = {"product-runtime-stop"}
_REQUIRED_LANDMARKS = {"header", "nav", "main"}
_REQUIRED_SHORTCUT_MARKERS = (
    'event.key === "F2"',
    'event.key === "F8"',
    "event.altKey",
    "event.ctrlKey",
    "event.metaKey",
    "event.shiftKey",
    "if (hasShortcutModifier(event)) return;",
)
_REQUIRED_EMERGENCY_STOP_KEYBOARD_MARKERS = (
    'getElementById("emergency-stop-action")',
    "button.addEventListener(\"click\", activateEmergencyStop);",
    "const dispatch = globalThis.autosportDispatch;",
    '"emergency_stop.activate",',
    "globalAnnouncement: false",
    "resultFocus: false",
    "postRefresh: false",
)
_FORBIDDEN_SHORTCUT_MARKERS = (
    'event.ctrlKey && event.altKey',
    'key === "r"',
    'key === "o"',
    'key === "e"',
    'key === "l"',
    'event.key === "F6"',
    'event.key === "F7"',
    'event.key === "F9"',
    'event.key === "F10"',
)
_REQUIRED_BRIDGE_MARKERS = (
    'window.addEventListener("pywebviewready"',
    'globalThis.pywebview.api.get_state()',
    'globalThis.pywebview.api.dispatch({',
    "const usePostRefresh = options.postRefresh !== false;",
    "if (usePostRefresh) await refreshState();",
    'dispatch("product_runtime.start")',
    'dispatch("product_runtime.stop")',
    'renderSingleColumnTable(byId("tickets-table-body"), state.tickets)',
)


class _SemanticShellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: dict[str, tuple[str, dict[str, str | None]]] = {}
        self.duplicate_ids: set[str] = set()
        self.duplicate_id_attributes: set[str] = set()
        self.id_references: list[tuple[str, str, str]] = []
        self.malformed_id_references: set[str] = set()
        self.labels_for: set[str] = set()
        self.landmarks: set[str] = set()
        self.h1_count = 0
        self.positive_tabindex: list[str] = []
        self.status_regions: list[dict[str, str | None]] = []
        self.alert_regions: list[dict[str, str | None]] = []
        # Static name evidence, distinct from the mere existence of a label/IDREF.
        self.element_text: dict[str, list[str]] = {}
        self.label_text: dict[str, list[str]] = {}
        # Separate IDREF text from implicit names: hidden descendants may
        # provide explicit aria-labelledby labels, but not implicit names.
        self.renderable_element_text: dict[str, list[str]] = {}
        self.renderable_label_text: dict[str, list[str]] = {}
        self.hidden_ancestor: dict[str, str] = {}
        self._open_tag_stack: list[tuple[str, str | None, str | None, bool]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        # Preserve all id attributes before dict() erases duplicate keys.
        # Both repeated attributes and repeated values make identity ambiguous.
        id_attributes = [value for name, value in attrs if name == "id"]
        values = dict(attrs)
        element_id = values.get("id")
        # This is only a bounded HTML source check, not computed CSS/UIA.
        inline_style = "".join((values.get("style") or "").lower().split())
        locally_hidden = (
            tag == "template"
            or "hidden" in values
            or "inert" in values
            or ("aria-hidden" in values and
                (values.get("aria-hidden") or "").strip().lower() != "false")
            or "display:none" in inline_style
            or "visibility:hidden" in inline_style
        )
        if element_id:
            for ancestor_tag, ancestor_id, _, ancestor_hidden in reversed(
                self._open_tag_stack
            ):
                if ancestor_hidden:
                    self.hidden_ancestor[element_id] = (
                        f"<{ancestor_tag}> id={ancestor_id or '(none)'}"
                    )
                    break
        if len(id_attributes) > 1:
            self.duplicate_id_attributes.add(element_id or tag)
        if element_id:
            if element_id in self.elements:
                self.duplicate_ids.add(element_id)
            self.elements[element_id] = (tag, values)
        # Validate static HTML IDREF destinations after the entire document is parsed.
        reference_attrs = ("aria-labelledby", "aria-describedby", "aria-controls")
        if tag == "label" and "for" in values:
            reference_attrs = (*reference_attrs, "for")
        for attribute in reference_attrs:
            if attribute not in values:
                continue
            targets = str(values[attribute] or "").split()
            if not targets or (attribute == "for" and len(targets) != 1):
                self.malformed_id_references.add(f"{element_id or tag}: {attribute}")
            for target in targets:
                self.id_references.append((element_id or tag, attribute, target))
        if tag in _REQUIRED_LANDMARKS:
            self.landmarks.add(tag)
        if tag == "h1":
            self.h1_count += 1
        if tag == "label" and values.get("for"):
            self.labels_for.add(str(values["for"]))
        tabindex = values.get("tabindex")
        if tabindex is not None:
            try:
                parsed = int(tabindex)
            except (TypeError, ValueError):
                parsed = 0
            if parsed > 0:
                self.positive_tabindex.append(element_id or tag)
        if values.get("role") == "status":
            self.status_regions.append(values)
        if values.get("role") == "alert":
            self.alert_regions.append(values)
        # HTML void elements must not retain spurious name-text scope.
        if tag not in {
            "area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr",
        }:
            label_target = str(values["for"]) if tag == "label" and values.get("for") else None
            self._open_tag_stack.append((tag, element_id, label_target, locally_hidden))

    def handle_data(self, data: str) -> None:
        # Visible-name text excludes hidden descendant subtrees. Preserve
        # unfiltered text solely for explicit aria-labelledby IDREFs.
        # Implicit names must exclude text hidden by ANY enclosing element,
        # including the label itself or its parent. Explicit aria-labelledby
        # references continue to use the separate unfiltered element_text.
        implicitly_visible = not any(frame[3] for frame in self._open_tag_stack)
        for _, element_id, label_target, _ in reversed(self._open_tag_stack):
            if element_id:
                self.element_text.setdefault(element_id, []).append(data)
                if implicitly_visible:
                    self.renderable_element_text.setdefault(element_id, []).append(data)
            if label_target:
                self.label_text.setdefault(label_target, []).append(data)
                if implicitly_visible:
                    self.renderable_label_text.setdefault(label_target, []).append(data)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._open_tag_stack) - 1, -1, -1):
            if self._open_tag_stack[index][0] == tag:
                del self._open_tag_stack[index:]
                return


def _reject_duplicate_html_identities(parser: _SemanticShellParser, failures: list[str]) -> None:
    if parser.duplicate_ids:
        failures.append("duplicate HTML id values: " + ", ".join(sorted(parser.duplicate_ids)))
    if parser.duplicate_id_attributes:
        failures.append("duplicate HTML id attributes: " + ", ".join(sorted(parser.duplicate_id_attributes)))


def _reject_broken_html_id_references(
    parser: _SemanticShellParser, failures: list[str]
) -> None:
    for reference in sorted(parser.malformed_id_references):
        failures.append(f"malformed HTML id reference: {reference}")
    for source, attribute, target in parser.id_references:
        if target not in parser.elements:
            failures.append(f"id={source}: {attribute} references missing id={target}")


def _reject_inaccessible_emergency_stop(
    parser: _SemanticShellParser, failures: list[str]
) -> None:
    """Reject emergency STOP controls hidden from or unreachable by keyboard/AT."""
    element = parser.elements.get("emergency-stop-action")
    if element is None:
        return
    _, attrs = element
    ancestor = parser.hidden_ancestor.get("emergency-stop-action")
    if ancestor:
        failures.append(
            "id=emergency-stop-action: inaccessible under hidden HTML ancestor "
            + ancestor
        )
    if "hidden" in attrs or "inert" in attrs:
        failures.append("id=emergency-stop-action: hidden/inert HTML state")
    for name in ("aria-hidden", "aria-disabled"):
        if name in attrs and (attrs[name] or "").strip().lower() != "false":
            failures.append(
                f"id=emergency-stop-action: {name} must be absent or false"
            )
    if "role" in attrs and (attrs["role"] or "").strip().lower() != "button":
        failures.append("id=emergency-stop-action: native button role overridden")
    if "tabindex" in attrs and (attrs["tabindex"] or "").strip() != "0":
        failures.append("id=emergency-stop-action: not in the normal Tab order")
    style = "".join((attrs.get("style") or "").lower().split())
    if "display:none" in style or "visibility:hidden" in style:
        failures.append("id=emergency-stop-action: hidden by inline CSS")


def _reject_missing_accessible_names(
    parser: _SemanticShellParser, failures: list[str]
) -> None:
    """Fail closed when critical controls have only structural/blank name evidence.

    This is a bounded static HTML-source contract, not physical NVDA testing
    or a claim to implement the entire browser accessible-name algorithm.
    """
    for automation_id, expected_tag in _REQUIRED_CONTROLS.items():
        if expected_tag not in {"button", "input", "select", "textarea"}:
            continue
        element = parser.elements.get(automation_id)
        if element is None:
            continue  # Missing mandatory IDs are reported by the caller.
        tag, attrs = element
        if tag != expected_tag:
            continue  # Wrong native role is reported by the caller.
        referenced = attrs.get("aria-labelledby")
        if referenced is not None:
            # aria-labelledby outranks an element's own text and other labels:
            # a blank reference must not be rescued by incidental button text.
            ids = referenced.split()
            named = bool(ids) and all(
                "".join(parser.element_text.get(ref, [])).strip() for ref in ids
            )
        elif (attrs.get("aria-label") or "").strip():
            named = True
        elif "".join(parser.renderable_label_text.get(automation_id, [])).strip():
            named = True
        else:
            named = tag == "button" and bool(
                "".join(parser.renderable_element_text.get(automation_id, [])).strip()
            )
        if not named:
            failures.append(
                f"id={automation_id}: missing nonblank accessible name/label"
            )


def _asset_paths() -> tuple[Path, Path]:
    index = web_shell_index_path()
    return index, index.with_name("app.js")


def _base_truth() -> dict[str, bool]:
    return {
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
    }


def inspect_semantic_shell() -> dict[str, Any]:
    index, script = _asset_paths()
    failures: list[str] = []
    try:
        html = index.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return {
            "status": "FAIL",
            "failures": [f"semantic shell HTML unreadable: {type(exc).__name__}: {exc}"],
            **_base_truth(),
        }

    parser = _SemanticShellParser()
    try:
        parser.feed(html)
        parser.close()
        _reject_duplicate_html_identities(parser, failures)
        _reject_broken_html_id_references(parser, failures)
        _reject_missing_accessible_names(parser, failures)
        _reject_inaccessible_emergency_stop(parser, failures)
    except Exception as exc:
        failures.append(f"semantic shell HTML parse failed: {type(exc).__name__}: {exc}")

    if parser.landmarks != _REQUIRED_LANDMARKS:
        failures.append(
            "required landmarks mismatch: "
            f"expected={sorted(_REQUIRED_LANDMARKS)!r} actual={sorted(parser.landmarks)!r}"
        )
    if parser.h1_count != 1:
        failures.append(f"semantic shell must contain exactly one h1, got {parser.h1_count}")
    if parser.positive_tabindex:
        failures.append(
            "positive tabindex is forbidden: " + ", ".join(parser.positive_tabindex)
        )
    if not any(region.get("aria-live") == "polite" for region in parser.status_regions):
        failures.append("semantic shell is missing a polite status live region")
    if not any(region.get("aria-live") == "assertive" for region in parser.alert_regions):
        failures.append("semantic shell is missing an assertive alert live region")

    live_status = parser.elements.get("live-status")
    if live_status is None:
        failures.append("live-status readback is missing")
    else:
        _, live_status_attrs = live_status
        if (
            live_status_attrs.get("role") == "status"
            or live_status_attrs.get("aria-live") not in {None, "off"}
        ):
            failures.append(
                "live-status must remain a non-announcing polled readback"
            )

    emergency_stop = parser.elements.get("emergency-stop-action")
    if emergency_stop is not None:
        _, emergency_attrs = emergency_stop
        described_by = set((emergency_attrs.get("aria-describedby") or "").split())
        required_description = {"emergency-stop-boundary", "emergency-stop-status"}
        if not required_description.issubset(described_by):
            failures.append(
                "emergency STOP must describe both durable safety boundary and current status"
            )
        if emergency_attrs.get("type") != "button":
            failures.append("emergency STOP must remain a native type=button action")
        if "disabled" in emergency_attrs:
            failures.append("emergency STOP must remain enabled in static shell semantics")

    emergency_status = parser.elements.get("emergency-stop-status")
    if emergency_status is None:
        failures.append("emergency STOP assertive status region is missing")
    else:
        _, emergency_status_attrs = emergency_status
        if (
            emergency_status_attrs.get("role") != "status"
            or emergency_status_attrs.get("aria-live") != "assertive"
            or emergency_status_attrs.get("aria-atomic") != "true"
        ):
            failures.append(
                "emergency STOP status must remain role=status, assertive, and atomic"
            )

    for automation_id, expected_tag in _REQUIRED_CONTROLS.items():
        element = parser.elements.get(automation_id)
        if element is None:
            failures.append(f"id={automation_id}: required semantic control missing")
            continue
        tag, attrs = element
        if tag != expected_tag:
            failures.append(
                f"id={automation_id}: expected native <{expected_tag}> got <{tag}>"
            )
        if tag in {"input", "select", "textarea"}:
            if automation_id not in parser.labels_for and not (attrs.get("aria-label") or "").strip():
                failures.append(f"id={automation_id}: form control has no label")
        if tag in {"button", "ul"} and not (attrs.get("aria-label") or "").strip():
            if tag == "ul":
                failures.append(f"id={automation_id}: dynamic list has no aria-label")
        if automation_id in _READONLY_CONTROLS and "readonly" not in attrs:
            failures.append(f"id={automation_id}: required readonly semantic state missing")
        if automation_id in _LIST_CONTROLS and attrs.get("tabindex") != "0":
            failures.append(f"id={automation_id}: evidence list is not keyboard focusable")
        if automation_id in _TABLE_CONTROLS and attrs.get("tabindex") != "0":
            failures.append(f"id={automation_id}: evidence table is not keyboard focusable")

    if '<caption>Паперові квитки і результати</caption>' not in html:
        failures.append("id=201: semantic tickets table is missing its caption")
    if '<th scope="col">' not in html:
        failures.append("id=201: semantic tickets table is missing a scoped column header")

    try:
        javascript = script.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        failures.append(
            f"semantic shell JavaScript unreadable: {type(exc).__name__}: {exc}"
        )
        javascript = ""

    for marker in _REQUIRED_BRIDGE_MARKERS:
        if marker not in javascript:
            failures.append(f"bridge marker missing: {marker}")
    if "String(error)" in javascript:
        failures.append("raw JavaScript bridge exception text must not reach accessible output")
    if (
        "syncRuntimeActionAvailability(" not in javascript
        or 'byId("product-runtime-start")' not in javascript
        or 'byId("product-runtime-stop")' not in javascript
    ):
        failures.append("runtime START/STOP dynamic enabled-state projection is missing")
    if (
        'setTextIfChanged(byId("manual-status"), state.manual.status || "")'
        not in javascript
    ):
        failures.append("manual-status live region projection is not change-only")
    if 'byId("manual-status").textContent = state.manual.status || ""' in javascript:
        failures.append("manual-status live region has an unconditional poll write")

    return {
        "status": "PASS" if not failures else "FAIL",
        "shell": "pywebview-edgechromium-semantic-html",
        "critical_control_count": len(_REQUIRED_CONTROLS),
        "landmarks": sorted(parser.landmarks),
        "h1_count": parser.h1_count,
        "positive_tabindex": list(parser.positive_tabindex),
        "status_live_regions": len(parser.status_regions),
        "alert_live_regions": len(parser.alert_regions),
        "failures": failures,
        "evidence_scope": (
            "static exact-package semantic HTML contract plus native-control/label/live-region "
            "inspection; paired release workflow still runs the external Windows UIA client "
            "against the packaged WebView2 process. This is not physical NVDA speech proof."
        ),
        **_base_truth(),
    }


def inspect_keyboard_contract() -> dict[str, Any]:
    index, script = _asset_paths()
    emergency_script = index.with_name("emergency_stop.js")
    failures: list[str] = []
    try:
        html = index.read_text(encoding="utf-8")
        javascript = script.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return {
            "status": "FAIL",
            "failures": [f"semantic keyboard assets unreadable: {type(exc).__name__}: {exc}"],
            **_base_truth(),
        }
    try:
        emergency_javascript = emergency_script.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        failures.append(
            "emergency STOP keyboard asset unreadable: "
            f"{type(exc).__name__}: {exc}"
        )
        emergency_javascript = ""

    parser = _SemanticShellParser()
    parser.feed(html)
    parser.close()
    _reject_duplicate_html_identities(parser, failures)
    _reject_broken_html_id_references(parser, failures)
    _reject_missing_accessible_names(parser, failures)
    _reject_inaccessible_emergency_stop(parser, failures)
    for marker in _REQUIRED_SHORTCUT_MARKERS:
        if marker not in javascript:
            failures.append(f"keyboard marker missing: {marker}")
    for marker in _FORBIDDEN_SHORTCUT_MARKERS:
        if marker in javascript:
            failures.append(f"screen-reader/browser shortcut collision remains: {marker}")
    if '<script src="emergency_stop.js"></script>' not in html:
        failures.append("emergency STOP keyboard asset is not loaded by the semantic shell")
    for marker in _REQUIRED_EMERGENCY_STOP_KEYBOARD_MARKERS:
        if marker not in emergency_javascript:
            failures.append(f"emergency STOP keyboard wiring marker missing: {marker}")
    modifier_guard = "if (hasShortcutModifier(event)) return;"
    modifier_guard_index = javascript.find(modifier_guard)
    for shortcut_marker in ('event.key === "F2"', 'event.key === "F8"'):
        shortcut_index = javascript.find(shortcut_marker)
        if (
            modifier_guard_index < 0
            or shortcut_index < 0
            or modifier_guard_index > shortcut_index
        ):
            failures.append(
                f"{shortcut_marker}: modifier fence must run before preventDefault handling"
            )
    if "replaceChildren()" in javascript:
        failures.append("poll projection must preserve semantic descendants when state is unchanged")
    for marker in (
        "setTextIfChanged(statusNode",
        "setTextIfChanged(errorNode",
        "syncTextChildren(node, values",
    ):
        if marker not in javascript:
            failures.append(f"stable dynamic projection marker missing: {marker}")

    focusable_ids = {
        element_id
        for element_id, (tag, attrs) in parser.elements.items()
        if (
            tag in {"button", "input", "select", "textarea"}
            or attrs.get("tabindex") == "0"
        )
        and "disabled" not in attrs
        and attrs.get("tabindex") != "-1"
    }
    missing_focusable = sorted(
        set(_REQUIRED_CONTROLS)
        - _DYNAMICALLY_DISABLED_CONTROLS
        - focusable_ids
    )
    if missing_focusable:
        failures.append(
            "critical controls missing from native/tab-focusable contract: "
            + ", ".join(missing_focusable)
        )
    for automation_id in _DYNAMICALLY_DISABLED_CONTROLS:
        element = parser.elements.get(automation_id)
        if element is None:
            continue
        tag, attrs = element
        if tag != "button" or "disabled" not in attrs:
            failures.append(
                f"id={automation_id}: expected initial disabled native button state"
            )
    if (
        "syncRuntimeActionAvailability(" not in javascript
        or 'byId("product-runtime-start")' not in javascript
        or 'byId("product-runtime-stop")' not in javascript
    ):
        failures.append("runtime START/STOP keyboard actionability projection is missing")
    if parser.positive_tabindex:
        failures.append("positive tabindex would override native DOM order")

    return {
        "status": "PASS" if not failures else "FAIL",
        "shell": "pywebview-edgechromium-semantic-html",
        "critical_focusable_controls": sorted(focusable_ids.intersection(_REQUIRED_CONTROLS)),
        "shortcut_markers": list(_REQUIRED_SHORTCUT_MARKERS),
        "positive_tabindex": list(parser.positive_tabindex),
        "failures": failures,
        "evidence_scope": (
            "machine inspection of native semantic focusability, DOM-order discipline, "
            "screen-reader/browser shortcut preservation, emergency STOP keyboard dispatch "
            "wiring, stable dynamic DOM projection, and declared keyboard shortcuts in the "
            "packaged WebView2 shell; not physical keyboard/NVDA speech proof."
        ),
        **_base_truth(),
    }


def run_accessibility_audit(output_path: str | Path) -> int:
    report = inspect_semantic_shell()
    atomic_write_json(Path(output_path), report)
    return 0 if report.get("status") == "PASS" else 1


def run_keyboard_audit(output_path: str | Path) -> int:
    report = inspect_keyboard_contract()
    atomic_write_json(Path(output_path), report)
    return 0 if report.get("status") == "PASS" else 1
