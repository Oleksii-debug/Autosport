"""Fixed-origin HTTP transport for ProphetX sandbox transaction history."""
from __future__ import annotations

from http.client import HTTPException
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .prophetx_account_readonly import ProphetXHttpResponse, ProphetXReadOnlyError

TRANSACTIONS_URL = "https://api.sandbox.prophetx.dev/partner/v4/mm/get_transactions"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ProphetXTransactionsTransport(Protocol):
    def get(
        self, url: str, *, headers: Mapping[str, str], timeout_seconds: float
    ) -> ProphetXHttpResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ProphetXReadOnlyError("ProphetX transaction redirect refused")


def _make_canonical_fetch(max_response_bytes: int):
    """Create the fixed-origin network primitive without exposing its opener."""

    # Capture the stdlib construction primitives in this closure once.  The
    # returned callable owns the opener and its bound open() method; callers do
    # not get a mutable opener attribute that can later be swapped underneath
    # provider-origin issuance.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    open_response = opener.open
    request_type = Request

    def fetch(
        url: str, *, headers: Mapping[str, str], timeout_seconds: float
    ) -> ProphetXHttpResponse:
        request = request_type(url, headers=dict(headers), method="GET")
        try:
            with open_response(request, timeout=timeout_seconds) as response:
                body = response.read(max_response_bytes + 1)
                if len(body) > max_response_bytes:
                    raise ProphetXReadOnlyError(
                        "ProphetX transaction response exceeded size limit"
                    )
                length = response.headers.get("Content-Length")
                if length is not None:
                    value = length.strip()
                    if (
                        not value.isascii()
                        or not value.isdigit()
                        or int(value) != len(body)
                    ):
                        raise ProphetXReadOnlyError(
                            "ProphetX transaction Content-Length is invalid"
                        )
                return ProphetXHttpResponse(
                    int(response.getcode()),
                    str(response.geturl()),
                    response.headers.get("Content-Type"),
                    response.headers.get("Content-Encoding"),
                    body,
                )
        except ProphetXReadOnlyError:
            raise
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise ProphetXReadOnlyError(
                f"ProphetX transaction HTTP status {status}"
            ) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise ProphetXReadOnlyError(
                "ProphetX transaction network request failed"
            ) from None

    return fetch


class UrllibProphetXTransactionsTransport:
    """GET-only sandbox transport; redirects/proxies/alternate endpoints are refused."""

    __slots__ = ("_max", "_fetch")

    def __init__(self, *, max_response_bytes: int = MAX_RESPONSE_BYTES) -> None:
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        self._max = max_response_bytes
        self._fetch = _make_canonical_fetch(max_response_bytes)

    @staticmethod
    def _validate_url(url: str) -> None:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise ProphetXReadOnlyError("invalid ProphetX transaction URL") from None
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.sandbox.prophetx.dev"
            or parsed.path != "/partner/v4/mm/get_transactions"
            or not parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
        ):
            raise ProphetXReadOnlyError(
                "transaction URL is outside the fixed ProphetX sandbox endpoint"
            )

    def get(
        self, url: str, *, headers: Mapping[str, str], timeout_seconds: float
    ) -> ProphetXHttpResponse:
        self._validate_url(url)
        return self._fetch(
            url,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
