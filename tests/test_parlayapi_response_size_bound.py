from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

import autosport.parlayapi_provider as provider_module
from autosport.parlayapi_provider import ProviderTransportError, _default_transport


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.status = 200
        self.headers: dict[str, str] = {}
        self.read_sizes: list[int] = []

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            return self._payload
        return self._payload[:size]


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def open(self, *_args: object, **_kwargs: object) -> _Response:
        return self.response


def test_default_transport_bounds_response_before_json_decode() -> None:
    response = _Response(b"x" * 32)
    with (
        patch.object(provider_module, "_MAX_RESPONSE_BYTES", 8),
        patch.object(provider_module, "build_opener", return_value=_Opener(response)),
    ):
        with pytest.raises(
            ProviderTransportError,
            match="provider response exceeded the size limit",
        ) as captured:
            _default_transport(
                "https://example.test/v1/sports/table_tennis/odds",
                {"X-API-Key": "dummy-key"},
                2.0,
            )

    assert response.read_sizes == [9]
    assert captured.value.status_code is None


def test_default_transport_accepts_exact_boundary_and_keeps_decimal_decode() -> None:
    payload = b'{"price":1.234567890123456789}'
    response = _Response(payload)
    with (
        patch.object(provider_module, "_MAX_RESPONSE_BYTES", len(payload)),
        patch.object(provider_module, "build_opener", return_value=_Opener(response)),
    ):
        result = _default_transport(
            "https://example.test/v1/sports/table_tennis/odds",
            {"X-API-Key": "dummy-key"},
            2.0,
        )

    assert response.read_sizes == [len(payload) + 1]
    assert result.payload == {"price": Decimal("1.234567890123456789")}
    assert result.status_code == 200
