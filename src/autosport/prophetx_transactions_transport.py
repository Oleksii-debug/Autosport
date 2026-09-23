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


class UrllibProphetXTransactionsTransport:
    """GET-only sandbox transport; redirects/proxies/alternate endpoints are refused."""

    def __init__(self, *, max_response_bytes: int = MAX_RESPONSE_BYTES) -> None:
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        self._max = max_response_bytes
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

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
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                body = response.read(self._max + 1)
                if len(body) > self._max:
                    raise ProphetXReadOnlyError(
                        "ProphetX transaction response exceeded size limit"
                    )
                length = response.headers.get("Content-Length")
                if length is not None:
                    value = length.strip()
                    if not value.isascii() or not value.isdigit() or int(value) != len(body):
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
