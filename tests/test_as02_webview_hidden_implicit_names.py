from __future__ import annotations

from autosport.windows_webview_audit import _SemanticShellParser, _reject_missing_accessible_names


def test_hidden_associated_label_does_not_name_critical_input() -> None:
    parser = _SemanticShellParser()
    parser.feed('<label for="205" hidden>Віртуальний банк</label><input id="205" type="text" readonly>')
    parser.close()
    failures: list[str] = []
    _reject_missing_accessible_names(parser, failures)
    assert 'id=205: missing nonblank accessible name/label' in failures
