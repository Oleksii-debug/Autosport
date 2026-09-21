from __future__ import annotations

from .localization import text


_WORKER_ERROR_TYPE = "WorkerError"


def _exception_type_name(exc: BaseException) -> str:
    """Return only a bounded type label without consulting exception metadata."""

    try:
        return type.__getattribute__(type(exc), "__name__")
    except BaseException:
        return "BaseException"


def safe_exception_text(exc: BaseException) -> str:
    """Render an exception for operator surfaces without exposing its detail.

    Exception strings are untrusted presentation input: provider libraries and
    transports commonly include URLs, headers, tokens, paths, or payload fragments
    in ``str(exc)``.  The GUI only needs a stable failure category because the
    surrounding localized message already identifies the failed operation.
    """

    return text(
        "ui.error.exception.message_unavailable",
        exception_type=_exception_type_name(exc),
    )


def safe_worker_error_text(_detail: object) -> str:
    """Render an opaque background-worker error without echoing worker detail."""

    return text(
        "ui.error.exception.message_unavailable",
        exception_type=_WORKER_ERROR_TYPE,
    )
