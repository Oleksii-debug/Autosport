from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re

import autosport.windows_webview_audit as webview_audit
from autosport.windows_webview_shell import web_shell_index_path


class _IdAttributeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.attrs_by_id: dict[str, dict[str, str | None]] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del tag
        values = dict(attrs)
        element_id = values.get("id")
        if element_id:
            self.attrs_by_id[element_id] = values


def _attributes() -> dict[str, dict[str, str | None]]:
    parser = _IdAttributeParser()
    parser.feed(web_shell_index_path().read_text(encoding="utf-8"))
    parser.close()
    return parser.attrs_by_id


def test_polled_live_market_status_is_readback_not_live_region() -> None:
    attrs = _attributes()

    # The market status is projected from the 250 ms state poll.  It must remain
    # readable on demand without creating an implicit announcement for every real
    # market-status transition.  Product announcements have their dedicated
    # bounded status/alert channels below.
    live_status = attrs["live-status"]
    assert live_status.get("role") != "status"
    assert live_status.get("aria-live") in {None, "off"}

    app_status = attrs["app-status"]
    assert app_status.get("role") == "status"
    assert app_status.get("aria-live") == "polite"

    error_status = attrs["error-status"]
    assert error_status.get("role") == "alert"
    assert error_status.get("aria-live") == "assertive"


def test_static_audit_rejects_reintroduced_live_status_announcement_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_index = web_shell_index_path()
    html = source_index.read_text(encoding="utf-8")
    script = source_index.with_name("app.js").read_text(encoding="utf-8")

    mutated, replacements = re.subn(
        r'<p id="live-status"[^>]*>',
        '<p id="live-status" role="status" aria-live="polite">',
        html,
        count=1,
    )
    assert replacements == 1

    index = tmp_path / "index.html"
    index.write_text(mutated, encoding="utf-8")
    (tmp_path / "app.js").write_text(script, encoding="utf-8")
    monkeypatch.setattr(webview_audit, "web_shell_index_path", lambda: index)

    report = webview_audit.inspect_semantic_shell()

    assert report["status"] == "FAIL"
    assert any("live-status" in failure for failure in report["failures"])
