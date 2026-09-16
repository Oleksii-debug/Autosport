import inspect

from autosport.windows_layout import (
    WINDOWS_SHELL_AUTOMATION_IDS,
    compact_surface_heights,
    configure_windows_product_shell_accessibility,
    install_windows_product_shell,
)


class _Widget:
    def __init__(self):
        self.height = None

    def configure(self, **kwargs):
        self.height = kwargs.get("height")


class _App:
    def __init__(self):
        self.live_quotes = _Widget()
        self.tickets = _Widget()
        self.evaluation = _Widget()
        self.log = _Widget()


def test_compact_surface_heights_keep_all_critical_scrolling_surfaces_visible():
    app = _App()

    compact_surface_heights(app)

    assert app.live_quotes.height == 4
    assert app.tickets.height == 5
    assert app.evaluation.height == 4
    assert app.log.height == 5


def test_windows_product_shell_has_stable_uia_ids_and_keyboard_navigation():
    assert WINDOWS_SHELL_AUTOMATION_IDS == {
        "navigation": 301,
        "state": 302,
        "open": 303,
        "details": 304,
    }

    build_source = inspect.getsource(install_windows_product_shell)
    for binding in (
        "<F2>",
        "<Control-Alt-Left>",
        "<Control-Alt-Right>",
        "<<ComboboxSelected>>",
    ):
        assert binding in build_source

    accessibility_source = inspect.getsource(configure_windows_product_shell_accessibility)
    for automation_id in WINDOWS_SHELL_AUTOMATION_IDS:
        assert f'WINDOWS_SHELL_AUTOMATION_IDS["{automation_id}"]' in accessibility_source
