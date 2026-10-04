"""Bind Wave M PaperBook loading to the canonical continuous-session global origin.

The materialization guard validates the canonical ``autosport.paper`` class/parser
surface, while ``ContinuousSessionCoordinator._load_book`` executes a LOAD_GLOBAL for
``continuous_session.PaperBook``. Keep that second binding non-authoritative by
witnessing it before and after the already-composed loader call. The later Wave M
dispatch guard seals this wrapper and its closure graph.
"""

from __future__ import annotations

from . import continuous_session as _session


def _install() -> None:
    session_module = _session
    coordinator = session_module.ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator, "__dict__")
    target = coordinator_dict["_load_book"]

    exact_type = type
    function_type = exact_type(_install)
    exact_getattr = getattr
    exact_len = len
    exact_zip = zip
    exact_value_error = ValueError
    missing = object()

    if exact_type(target) is not function_type:
        raise TypeError("canonical continuous-session PaperBook loader is unavailable")

    target_code = target.__code__
    target_closure = target.__closure__ or ()
    if exact_len(target_closure) != exact_len(target_code.co_freevars):
        raise TypeError("canonical continuous-session PaperBook loader closure is malformed")
    target_cells = tuple((cell, cell.cell_contents) for cell in target_closure)

    session_namespace = exact_type(session_module).__getattribute__(
        session_module,
        "__dict__",
    )
    canonical_paper_book = session_namespace.get("PaperBook", missing)
    failure_type = session_namespace.get("ContinuousSessionError", missing)
    if canonical_paper_book is missing or exact_type(failure_type) is not type:
        raise TypeError("canonical continuous-session PaperBook authority is unavailable")

    def require_authority() -> None:
        if session_namespace.get("PaperBook", missing) is not canonical_paper_book:
            raise failure_type("continuous-session PaperBook load authority changed")
        if exact_getattr(target, "__code__", None) is not target_code:
            raise failure_type(
                "canonical continuous-session PaperBook loader executable changed"
            )
        current_closure = exact_getattr(target, "__closure__", None) or ()
        if exact_len(current_closure) != exact_len(target_cells):
            raise failure_type(
                "canonical continuous-session PaperBook loader closure changed"
            )
        for current_cell, (expected_cell, expected_value) in exact_zip(
            current_closure,
            target_cells,
        ):
            if current_cell is not expected_cell:
                raise failure_type(
                    "canonical continuous-session PaperBook loader closure changed"
                )
            try:
                current_value = current_cell.cell_contents
            except exact_value_error as exc:
                raise failure_type(
                    "canonical continuous-session PaperBook loader closure changed"
                ) from exc
            if current_value is not expected_value:
                raise failure_type(
                    "canonical continuous-session PaperBook loader closure changed"
                )

    def guarded_load_book(self):
        require_authority()
        book = target(self)
        require_authority()
        return book

    guarded_load_book.__name__ = target.__name__
    guarded_load_book.__qualname__ = target.__qualname__
    guarded_load_book.__doc__ = target.__doc__
    guarded_load_book.__annotations__ = target.__annotations__

    type.__setattr__(coordinator, "_load_book", guarded_load_book)


_install()
del _install
