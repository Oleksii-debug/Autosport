from __future__ import annotations

import ast
import re
from pathlib import Path
from string import Formatter

from autosport.localization import DEFAULT_LOCALE, catalog


_REPO_ROOT = Path(__file__).resolve().parents[1]
_REQUIRED_CRITICAL_PRESENTATION_MODULES = (
    Path("src/autosport/gui.py"),
    Path("src/autosport/windows_gui.py"),
    Path("src/autosport/windows_layout.py"),
    Path("src/autosport/windows_manual_calculation.py"),
)
_OPTIONAL_CRITICAL_PRESENTATION_MODULES = (
    # This packaged UIA surface is introduced by a separate accessibility lineage.
    # Once present on the tested tree it must automatically fall under this gate;
    # the static gate must not require a follow-up edit just to notice the file.
    Path("src/autosport/windows_accessible_gui.py"),
)
_DIRECT_PRESENTATION_LITERAL_ALLOWLIST_V1 = frozenset()
_CYRILLIC_RE = re.compile(r"[А-ЩЬЮЯЄІЇҐа-щьюяєіїґ]")
_HUMAN_TRUTH_PROMOTION_RE = re.compile(
    r"\b(?:HUMAN_TESTED|NVDA_VERIFIED)\s*=\s*(?:true|1)\b",
    re.IGNORECASE,
)
_CRITICAL_HUMAN_KEYS = frozenset(
    {
        "ui.app.title",
        "ui.button.choose_dataset",
        "ui.button.run_replay",
        "ui.button.repair_workspace",
        "ui.button.live_refresh",
        "ui.status.startup.recovery_required",
        "ui.status.recovery.failed_until_fixed",
        "ui.accessibility.choose_dataset.name",
        "ui.accessibility.choose_dataset.description",
        "ui.accessibility.run_replay.name",
        "ui.accessibility.run_replay.description",
        "ui.accessibility.repair_workspace.name",
        "ui.accessibility.repair_workspace.description",
        "ui.accessibility.live_refresh.name",
        "ui.accessibility.live_refresh.description",
        "ui.windows.shell.accessibility.navigation.name",
        "ui.windows.shell.accessibility.navigation.description",
        "ui.windows.owner_authority.accessibility.open.name",
        "ui.windows.owner_authority.accessibility.open.description",
        "ui.windows.manual_calculation.uia.input.name",
        "ui.windows.manual_calculation.uia.input.description",
        "ui.windows.manual_calculation.uia.calculate.name",
        "ui.windows.manual_calculation.uia.calculate.description",
        "ui.windows.manual_calculation.status.error",
    }
)


def _critical_presentation_modules() -> tuple[Path, ...]:
    required_missing = [
        path
        for path in _REQUIRED_CRITICAL_PRESENTATION_MODULES
        if not (_REPO_ROOT / path).is_file()
    ]
    assert not required_missing, f"missing required critical presentation modules: {required_missing}"
    optional_present = tuple(
        path
        for path in _OPTIONAL_CRITICAL_PRESENTATION_MODULES
        if (_REPO_ROOT / path).is_file()
    )
    return _REQUIRED_CRITICAL_PRESENTATION_MODULES + optional_present


def _source(relative_path: Path) -> str:
    return (_REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _tree(relative_path: Path) -> ast.AST:
    return ast.parse(_source(relative_path), filename=str(relative_path))


def _literal_text_keys(relative_path: Path) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(_tree(relative_path)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "text":
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            keys.add(first.value)
    return keys


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = [func.attr]
        value = func.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
        return ".".join(reversed(parts))
    return None


def _static_string(
    node: ast.AST | None,
    constants: dict[str, str],
) -> str | None:
    """Resolve only statically-known strings used directly at presentation sinks.

    The gate deliberately does not evaluate arbitrary Python. It covers the
    bounded escape forms that can otherwise hide hard-coded presentation copy
    from an inline-literal check: literal f-strings, constant concatenation and
    module-level string constants (including constants composed from other
    already-resolved constants).
    """

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        pieces: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            pieces.append(value.value)
        return "".join(pieces)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left, constants)
        right = _static_string(node.right, constants)
        if left is not None and right is not None:
            return left + right
        return None
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _module_string_constants(tree: ast.AST) -> dict[str, str]:
    if not isinstance(tree, ast.Module):
        return {}

    candidates: dict[str, ast.AST] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    candidates[target.id] = statement.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.value is not None
        ):
            candidates[statement.target.id] = statement.value

    resolved: dict[str, str] = {}
    pending = dict(candidates)
    while pending:
        progressed = False
        for name, expression in tuple(pending.items()):
            value = _static_string(expression, resolved)
            if value is None:
                continue
            resolved[name] = value
            del pending[name]
            progressed = True
        if not progressed:
            break
    return resolved


def _presentation_literal_violations_in_tree(
    tree: ast.AST,
    source_name: str,
) -> list[str]:
    violations: list[str] = []
    constants = _module_string_constants(tree)
    widget_calls = {
        "tk.Button",
        "tk.Checkbutton",
        "tk.Label",
        "tk.LabelFrame",
        "tk.Message",
        "tk.Radiobutton",
        "ttk.Button",
        "ttk.Checkbutton",
        "ttk.Label",
        "ttk.LabelFrame",
        "ttk.Radiobutton",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        candidates: list[tuple[str, ast.AST | None]] = []

        if name and name.endswith(".title") and node.args:
            candidates.append(("window title", node.args[0]))
        if name and name.startswith("messagebox."):
            for index, arg in enumerate(node.args[:2]):
                candidates.append((f"{name} arg{index + 1}", arg))
            for keyword in node.keywords:
                if keyword.arg in {"title", "message", "detail"}:
                    candidates.append((f"{name} {keyword.arg}", keyword.value))
        if name in {"tk_uia.set_acc_name", "tk_uia.set_acc_description"} and len(node.args) >= 2:
            candidates.append((name, node.args[1]))
        if name in widget_calls:
            for keyword in node.keywords:
                if keyword.arg == "text":
                    candidates.append((f"{name} text", keyword.value))

        for sink, candidate in candidates:
            literal = _static_string(candidate, constants)
            if literal and literal not in _DIRECT_PRESENTATION_LITERAL_ALLOWLIST_V1:
                violations.append(
                    f"{source_name}:{getattr(node, 'lineno', '?')}: "
                    f"{sink} bypasses localization catalog: {literal!r}"
                )
    return violations


def _presentation_literal_violations(relative_path: Path) -> list[str]:
    return _presentation_literal_violations_in_tree(
        _tree(relative_path),
        str(relative_path),
    )


def test_default_locale_and_critical_human_copy_are_ukrainian() -> None:
    assert DEFAULT_LOCALE == "uk-UA"
    messages = catalog(DEFAULT_LOCALE)

    missing = sorted(_CRITICAL_HUMAN_KEYS - set(messages))
    assert not missing, f"missing critical uk-UA localization keys: {missing}"

    empty = sorted(key for key in _CRITICAL_HUMAN_KEYS if not messages[key].strip())
    assert not empty, f"empty critical uk-UA localization keys: {empty}"

    non_ukrainian = sorted(
        key for key in _CRITICAL_HUMAN_KEYS if not _CYRILLIC_RE.search(messages[key])
    )
    assert not non_ukrainian, (
        "critical human-facing resources must contain Ukrainian text; "
        f"machine/domain identifiers may remain language-neutral: {non_ukrainian}"
    )


def test_every_literal_catalog_reference_on_critical_windows_surfaces_exists() -> None:
    messages = catalog(DEFAULT_LOCALE)
    referenced: set[str] = set()
    for relative_path in _critical_presentation_modules():
        referenced.update(_literal_text_keys(relative_path))

    assert referenced, "critical presentation modules exposed no literal localization references"

    missing = sorted(key for key in referenced if key not in messages)
    assert not missing, f"critical Windows surfaces reference missing localization keys: {missing}"

    empty = sorted(key for key in referenced if not messages[key].strip())
    assert not empty, f"critical Windows surfaces reference empty localization values: {empty}"

    malformed: list[str] = []
    formatter = Formatter()
    for key in sorted(referenced):
        try:
            list(formatter.parse(messages[key]))
        except ValueError as exc:
            malformed.append(f"{key}: {exc}")
    assert not malformed, f"malformed localization format strings: {malformed}"


def test_critical_presentation_sinks_do_not_bypass_localization_catalog() -> None:
    violations: list[str] = []
    modules = _critical_presentation_modules()
    assert Path("src/autosport/windows_gui.py") in modules
    for relative_path in modules:
        violations.extend(_presentation_literal_violations(relative_path))
    assert not violations, "\n".join(violations)


def test_static_gate_rejects_indirect_and_expression_literal_escape_forms() -> None:
    fixture = ast.parse(
        '''
import tk_uia
from tkinter import messagebox

STATUS_NAME = "Hard" + " coded status"
STATUS_DESCRIPTION = "Hard-coded description"

tk_uia.set_acc_name(widget, STATUS_NAME)
tk_uia.set_acc_description(widget, STATUS_DESCRIPTION)
messagebox.showerror(f"English title", "English " + "message")
'''
    )
    violations = _presentation_literal_violations_in_tree(fixture, "fixture.py")

    assert len(violations) == 4
    assert any("Hard coded status" in violation for violation in violations)
    assert any("Hard-coded description" in violation for violation in violations)
    assert any("English title" in violation for violation in violations)
    assert any("English message" in violation for violation in violations)


def test_static_gate_allows_canonical_catalog_resolution_at_uia_sinks() -> None:
    fixture = ast.parse(
        '''
import tk_uia
from autosport.localization import text

tk_uia.set_acc_name(widget, text("ui.accessibility.operational_status.name"))
tk_uia.set_acc_description(
    widget,
    text("ui.accessibility.operational_status.description"),
)
'''
    )

    assert not _presentation_literal_violations_in_tree(fixture, "fixture.py")


def test_ukrainian_catalog_round_trips_through_cyrillic_path(tmp_path: Path) -> None:
    messages = catalog(DEFAULT_LOCALE)
    folder = tmp_path / "Тестова робоча область з пробілами"
    folder.mkdir()
    target = folder / "український стан.txt"

    payload = "\n".join(messages[key] for key in sorted(_CRITICAL_HUMAN_KEYS)) + "\n"
    target.write_text(payload, encoding="utf-8")

    assert target.read_text(encoding="utf-8") == payload
    assert _CYRILLIC_RE.search(target.name)
    assert _CYRILLIC_RE.search(str(folder.name))


def test_machine_localization_gate_cannot_promote_human_or_nvda_truth() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    assert not _HUMAN_TRUTH_PROMOTION_RE.search(source)
