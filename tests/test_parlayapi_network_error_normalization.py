from __future__ import annotations

from unittest.mock import patch

import pytest

import autosport.parlayapi_provider as provider_module
from autosport.parlayapi_provider import ProviderTransportError, _default_transport


class _ReadFailureResponse:
    status = 200
    headers: dict[str, str] = {}

    def __enter__(self) -> "_ReadFailureResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        raise OSError("sensitive low-level read detail")


class _ReadFailureOpener:
    def open(self, *_args: object, **_kwargs: object) -> _ReadFailureResponse:
        return _ReadFailureResponse()


class _OpenTimeoutOpener:
    def open(self, *_args: object, **_kwargs: object):
        raise TimeoutError("sensitive low-level timeout detail")


@pytest.mark.parametrize(
    "opener",
    (_OpenTimeoutOpener(), _ReadFailureOpener()),
)
def test_default_transport_normalizes_raw_network_os_errors(opener: object) -> None:
    with patch.object(provider_module, "build_opener", return_value=opener):
        with pytest.raises(ProviderTransportError) as captured:
            _default_transport(
                "https://example.test/v1/sports/table_tennis/odds",
                {"X-API-Key": "dummy-key"},
                2.0,
            )

    assert str(captured.value) == "provider transport error"
    assert captured.value.status_code is None
    assert "sensitive low-level" not in str(captured.value)
