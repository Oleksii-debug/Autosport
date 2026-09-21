from __future__ import annotations

import importlib.util
from pathlib import Path


_GUARD_PATH = Path(__file__).with_name("test_ukrainian_presentation_sink_guard.py")
_SPEC = importlib.util.spec_from_file_location(
    "autosport_ukrainian_presentation_sink_guard",
    _GUARD_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
_GUARD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_GUARD)


def test_guard_rejects_composed_hardcoded_presentation_copy(tmp_path, monkeypatch) -> None:
    target = tmp_path / "src" / "autosport" / "gui.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def render(widget, name):\n"
        "    widget.configure(text=\"English prefix: \" + name)\n"
        "    widget.configure(text=\"English value: {}\".format(name))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_GUARD, "_REPO_ROOT", tmp_path)

    violations = _GUARD._direct_presentation_literals(
        Path("src/autosport/gui.py")
    )

    # Both expressions contain direct product-owned presentation copy at a
    # guarded text= sink and bypass autosport.localization.text(). Looking only
    # at the expression's top-level AST type (BinOp/Call) must not hide the
    # embedded string literal from the regression fence.
    assert len(violations) == 2
