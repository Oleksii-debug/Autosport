from __future__ import annotations

import ast
import inspect
import re
import textwrap
import unittest
from html.parser import HTMLParser
from pathlib import Path

from autosport.windows_webview_shell import AutosportWebController, web_shell_index_path


class _ElementInventory(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: dict[str, tuple[str, dict[str, str | None]]] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        identity = attributes.get("id")
        if identity is not None:
            self.elements[str(identity)] = (tag, attributes)


def _success_focus_ids(method) -> tuple[str, ...]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    identities: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute)
            and function.attr == "_ok"
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg != "focus_id":
                continue
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                identities.append(keyword.value.value)
    return tuple(identities)


def _busy_disabled_ids() -> frozenset[str]:
    script = (
        Path(web_shell_index_path()).with_name("app.js").read_text(encoding="utf-8")
    )
    match = re.search(
        r"\[([0-9,\s]+)\]\.forEach\(\(id\)\s*=>\s*\{\s*"
        r"setDisabledWithFocusFallback\(\s*byId\(id\),\s*busyDisabled\s*\);",
        script,
    )
    if match is None:
        raise AssertionError("busy-disabled control inventory was not found")
    return frozenset(part.strip() for part in match.group(1).split(",") if part.strip())


def _is_programmatically_focusable(tag: str, attrs: dict[str, str | None]) -> bool:
    if "disabled" in attrs:
        return False
    if tag in {"button", "input", "select", "textarea"}:
        return True
    if tag == "a" and attrs.get("href"):
        return True
    return attrs.get("tabindex") in {"0", "-1"}


class WindowsWebViewAsyncBusyFocusHandoffTests(unittest.TestCase):
    def test_successful_async_actions_handoff_focus_before_invoker_is_disabled(self) -> None:
        methods = {
            "replay": AutosportWebController._action_replay_run,
            "live refresh": AutosportWebController._action_live_refresh,
            "recovery": AutosportWebController._action_recovery_run,
            "evidence export": AutosportWebController._action_evidence_export,
        }
        inventory = _ElementInventory()
        inventory.feed(Path(web_shell_index_path()).read_text(encoding="utf-8"))
        busy_disabled = _busy_disabled_ids()

        for label, method in methods.items():
            with self.subTest(action=label):
                focus_ids = _success_focus_ids(method)
                self.assertTrue(
                    focus_ids,
                    f"{label} starts asynchronous work but returns no stable focus_id",
                )
                for focus_id in focus_ids:
                    self.assertNotIn(
                        focus_id,
                        busy_disabled,
                        f"{label} hands focus to a control disabled while work is busy",
                    )
                    self.assertIn(focus_id, inventory.elements)
                    tag, attrs = inventory.elements[focus_id]
                    self.assertTrue(
                        _is_programmatically_focusable(tag, attrs),
                        f"{label} focus target {focus_id!r} is not programmatically focusable",
                    )

    def test_runtime_actions_are_nonvacuous_positive_focus_handoff_controls(self) -> None:
        for method in (
            AutosportWebController._action_product_runtime_start,
            AutosportWebController._action_product_runtime_stop,
        ):
            self.assertEqual(_success_focus_ids(method), ("product-runtime-status",))


if __name__ == "__main__":
    unittest.main()
