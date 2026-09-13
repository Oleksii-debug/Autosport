from autosport.windows_layout import compact_surface_heights


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
