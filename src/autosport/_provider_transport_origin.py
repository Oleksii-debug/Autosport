from __future__ import annotations

"""Pin authenticated complete-board transport to the canonical provider origin.

``urllib.request.urlopen`` follows HTTP redirects by default. The complete-board
request carries ``X-API-Key``, so following a 30x before application-level response
validation could forward that credential to another origin and let the redirected
response participate in positive completeness acquisition.

Install a module-local replacement only for ``provider_observation_authority``. It
uses an opener whose redirect handler never creates a follow-up request and also
checks the original request is the exact production HTTPS authority before any
transport call is made. Other Autosport urllib users are deliberately unaffected.
"""

from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import provider_observation_authority as provider


_PROVIDER_SCHEME = "https"
_PROVIDER_NETLOC = "parlay-api.com"
_PROVIDER_PATH_PREFIX = "/v1/sse/odds/"
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class _RejectProviderRedirects(HTTPRedirectHandler):
    """Never construct a follow-up request for an HTTP redirect response."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, msg, headers, newurl
        if code in _REDIRECT_CODES:
            return None
        return None


def _canonical_provider_urlopen(request: Request, *, timeout: float):
    """Open only the canonical provider request with redirects disabled."""

    if not isinstance(request, Request):
        raise provider.ProviderObservationUnsupportedError(
            "provider SSE transport requires an urllib Request"
        )
    parsed = urlsplit(request.full_url)
    if (
        parsed.scheme != _PROVIDER_SCHEME
        or parsed.netloc != _PROVIDER_NETLOC
        or not parsed.path.startswith(_PROVIDER_PATH_PREFIX)
        or parsed.fragment
    ):
        raise provider.ProviderObservationUnsupportedError(
            "provider SSE transport origin is not the canonical Parlay HTTPS endpoint"
        )
    if request.get_method() != "GET":
        raise provider.ProviderObservationUnsupportedError(
            "provider SSE transport requires GET"
        )

    opener = build_opener(_RejectProviderRedirects())
    return opener.open(request, timeout=timeout)


def _install_guard() -> None:
    if getattr(provider.urlopen, "_canonical_provider_no_redirect_guard", False):
        return
    setattr(_canonical_provider_urlopen, "_canonical_provider_no_redirect_guard", True)
    provider.urlopen = _canonical_provider_urlopen


_install_guard()
del _install_guard

__all__: list[str] = []
