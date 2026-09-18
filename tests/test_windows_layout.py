import inspect

from autosport.windows_layout import (
    WINDOWS_SHELL_AUTOMATION_IDS,
    WINDOWS_SHELL_DETAILS_VISIBLE_ROWS,
    compact_surface_heights,
    configure_windows_product_shell_accessibility,
    install_compact_windows_layout,
    install_windows_product_shell,
    refresh_windows_shell_open_availability,
    _surface_target_widget,
)


class _Widget:
    def __init__(self):
        self.height = None
        self.pady = None

    def configure(self, **kwargs):
        self.height = kwargs.get("height")

    def pack_configure(self, **kwargs):
        self.pady = kwargs.get("pady")


class _App:
    def __init__(self):
        self.live_quotes = _Widget()
        self.tickets = _Widget()
        self.evaluation = _Widget()
        self.log = _Widget()
        self.tickets_label = _Widget()
        self.evaluation_label = _Widget()
        self.log_label = _Widget()


def test_compact_surface_heights_keep_all_critical_scrolling_surfaces_visible():
    app = _App()

    compact_surface_heights(app)

    assert app.live_quotes.height == 3
    assert app.tickets.height == 4
    assert app.evaluation.height == 3
    assert app.log.height == 2
    assert app.tickets_label.pady == (6, 2)
    assert app.evaluation_label.pady == (6, 2)
    assert app.log_label.pady == (6, 2)
    assert WINDOWS_SHELL_DETAILS_VISIBLE_ROWS == 1


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

    install_source = inspect.getsource(install_compact_windows_layout)
    assert "refresh_windows_shell_open_availability(self)" in install_source


def test_shell_target_requires_a_real_widget_before_open_is_authorized():
    class SurfaceKey:
        def get(self):
            return "paper_bank"

    class OpenButton:
        def __init__(self):
            self.state = None

        def configure(self, **kwargs):
            self.state = kwargs["state"]

    class App:
        shell_surface_key = SurfaceKey()
        shell_open_button = OpenButton()

    app = App()

    # The shell is installed by the patched base build before WindowsAutosportApp
    # creates its late-bound bank_summary. Persisted Paper Bank must fail closed
    # at that first render rather than route to an unrelated control.
    assert _surface_target_widget(app, "paper_bank") is None
    refresh_windows_shell_open_availability(app)
    assert app.shell_open_button.state == "disabled"

    # The accessibility-configuration phase runs after the subclass build has
    # created bank_summary. Re-checking authority must make Open usable without
    # changing the persisted surface selection.
    bank_summary = object()
    app.bank_summary = bank_summary
    assert _surface_target_widget(app, "paper_bank") is bank_summary
    refresh_windows_shell_open_availability(app)
    assert app.shell_open_button.state == "normal"
