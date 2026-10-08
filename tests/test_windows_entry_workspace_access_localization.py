from __future__ import annotations

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_WINDOWS_ENTRY = _REPO_ROOT / "src" / "autosport" / "windows_entry.py"
_REQUIRED_KEYS = {
    "_show_workspace_configuration_error": "ui.windows.workspace_configuration.title",
    "_workspace_access_error_message": "ui.windows.workspace_access.message",
    "_show_workspace_access_error": "ui.windows.workspace_access.title",
}


def _target_functions() -> dict[str, ast.FunctionDef]:
    source = _WINDOWS_ENTRY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_WINDOWS_ENTRY))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in _REQUIRED_KEYS
    }
    assert set(functions) == set(_REQUIRED_KEYS), (
        "workspace-access presentation helpers changed without updating the "
        "canonical-localization contract"
    )
    return functions


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _localization_keys(function: ast.FunctionDef) -> set[str]:
    keys: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or _call_name(node) != "text" or not node.args:
            continue
        key = node.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
    return keys


def _direct_product_copy(function: ast.FunctionDef) -> list[str]:
    docstring_literal: ast.Constant | None = None
    if (
        function.body
        and isinstance(function.body[0], ast.Expr)
        and isinstance(function.body[0].value, ast.Constant)
        and isinstance(function.body[0].value.value, str)
    ):
        docstring_literal = function.body[0].value

    violations: list[str] = []
    for node in ast.walk(function):
        if node is docstring_literal:
            continue
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        literal = node.value.strip()
        if not literal or literal.startswith("ui."):
            continue
        if any(character.isalpha() for character in literal):
            violations.append(f"line {node.lineno}: {node.value!r}")
    return violations


def test_native_workspace_access_error_uses_canonical_localization_catalog() -> None:
    functions = _target_functions()

    for function_name, required_key in _REQUIRED_KEYS.items():
        function = functions[function_name]
        keys = _localization_keys(function)
        assert required_key in keys, (
            f"{function_name} must resolve product-owned native-dialog copy through "
            f"autosport.localization.text({required_key!r}); observed keys={sorted(keys)!r}"
        )
        assert _direct_product_copy(function) == [], (
            f"{function_name} contains direct alphabetic product copy outside the canonical "
            "localization boundary:\n"
            + "\n".join(_direct_product_copy(function))
        )

def test_workspace_access_message_renders_canonical_catalog_values() -> None:
    from autosport.localization import text
    from autosport.windows_entry import _workspace_access_error_message

    workspace = Path("C:/Users/test/AppData/Local/Autosport/workspace")
    error = OSError("Access denied\nsecond line")

    message = _workspace_access_error_message(workspace, error)

    assert message == text(
        "ui.windows.workspace_access.message",
        workspace=workspace,
        error_type="OSError",
        error_detail="Access denied second line",
    )
    assert "{workspace}" not in message
    assert "{error_type}" not in message
    assert "{error_detail}" not in message


def test_workspace_access_message_uses_localized_unknown_error_fallback() -> None:
    from autosport.localization import text
    from autosport.windows_entry import _workspace_access_error_message

    workspace = Path("C:/Users/test/AppData/Local/Autosport/workspace")
    fallback = text("ui.windows.workspace_access.unknown_error")

    message = _workspace_access_error_message(workspace, OSError())

    assert message == text(
        "ui.windows.workspace_access.message",
        workspace=workspace,
        error_type="OSError",
        error_detail=fallback,
    )
    assert fallback in message



def test_native_workspace_configuration_dialog_uses_catalog_and_keeps_native_fallback(
    monkeypatch,
) -> None:
    import ctypes
    from types import SimpleNamespace

    from autosport.localization import text
    from autosport.windows_entry import _show_workspace_configuration_error

    calls: list[tuple[object, str, str, int]] = []

    def message_box(parent: object, message: str, title: str, flags: int) -> int:
        calls.append((parent, message, title, flags))
        return 1

    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(user32=SimpleNamespace(MessageBoxW=message_box)),
        raising=False,
    )
    detail = r"Недійсний шлях C:\Користувачі\Тест"
    _show_workspace_configuration_error(detail)

    assert calls == [
        (
            None,
            text("ui.windows.workspace_configuration.message", detail=detail),
            text("ui.windows.workspace_configuration.title"),
            0x00000010,
        )
    ]
    assert "interactive workspace" not in calls[0][1]
    assert "Economic і live state" not in calls[0][1]


def test_native_configuration_dialog_requires_message_catalog_key() -> None:
    function = _target_functions()["_show_workspace_configuration_error"]
    assert "ui.windows.workspace_configuration.message" in _localization_keys(function)

def test_native_workspace_errors_redact_secrets_and_preserve_ukrainian_guidance() -> None:
    from autosport.windows_entry import _workspace_access_error_message
    from autosport.secret_redaction import REDACTED

    workspace = Path("C:/Папка?api_key=workspace-secret-1842")
    error = OSError("Access denied; password=provider-secret-1843; token=private-1844")
    rendered = _workspace_access_error_message(workspace, error)

    assert "workspace-secret-1842" not in rendered
    assert "provider-secret-1843" not in rendered
    assert "private-1844" not in rendered
    assert REDACTED in rendered
    assert "Робоча тека" in rendered
    assert "Права адміністратора не потрібні" in rendered


def test_native_workspace_error_stringification_failure_is_localized_and_safe() -> None:
    from autosport.localization import text
    from autosport.windows_entry import _workspace_access_error_message

    class HostileOSError(OSError):
        def __str__(self) -> str:
            raise RuntimeError("token=must-not-appear")

    rendered = _workspace_access_error_message(Path("C:/Робоча тека"), HostileOSError())
    assert text("ui.windows.workspace_access.unknown_error") in rendered
    assert "must-not-appear" not in rendered
    assert "HostileOSError" not in rendered
    assert "OSError" in rendered


def test_native_configuration_dialog_redacts_untrusted_detail(monkeypatch) -> None:
    import ctypes
    from types import SimpleNamespace
    from autosport.windows_entry import _show_workspace_configuration_error

    calls = []
    def message_box(parent, message, title, flags):
        calls.append((message, title, flags))
        return 1

    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(
        user32=SimpleNamespace(MessageBoxW=message_box)
    ), raising=False)
    _show_workspace_configuration_error("Невірний шлях; password=config-secret-1845")
    assert len(calls) == 1
    assert "config-secret-1845" not in calls[0][0]
    assert "[REDACTED]" in calls[0][0]
    assert "Автоспорт" in calls[0][1]
    assert calls[0][2] == 0x00000010
