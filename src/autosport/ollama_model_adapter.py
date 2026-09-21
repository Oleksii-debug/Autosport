"""Concrete loopback Ollama adapter for the optional model invocation boundary.

This module is deliberately subordinate to :mod:`autosport.model_invocation`.
It owns only the HTTP translation for a locally configured Ollama ``/api/chat``
endpoint. It grants no provider, economic, risk, execution, settlement,
scientific-promotion, or UI authority.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import socket
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .model_invocation import (
    ModelAdapterDescriptor,
    ModelAdapterInvalidResponse,
    ModelAdapterResponse,
    ModelBackendMode,
    ModelInvocationError,
    ModelInvocationRequest,
)

_DEFAULT_MAX_RESPONSE_BYTES: Final[int] = 1_048_576
_MIN_RESPONSE_BYTES: Final[int] = 1_024
_MAX_RESPONSE_BYTES: Final[int] = 8 * 1_048_576
_BACKEND_ID: Final[str] = "ollama-local-http"
_ENDPOINT_CLASS: Final[str] = "LOCAL_LOOPBACK_HTTP"


def _canonical_text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ModelInvocationError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8", errors="strict")
    return value


def _validate_loopback_chat_endpoint(value: object) -> str:
    endpoint = _canonical_text("endpoint", value)
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise ModelInvocationError("endpoint is invalid") from exc

    if parsed.scheme != "http":
        raise ModelInvocationError("Ollama endpoint must use http")
    if not parsed.hostname:
        raise ModelInvocationError("Ollama endpoint must include a loopback host")
    if parsed.username is not None or parsed.password is not None:
        raise ModelInvocationError("Ollama endpoint must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ModelInvocationError("Ollama endpoint must not contain query or fragment")
    if parsed.path != "/api/chat":
        raise ModelInvocationError("Ollama endpoint path must be /api/chat")
    if port is not None and not 1 <= port <= 65535:
        raise ModelInvocationError("Ollama endpoint port is invalid")

    host = parsed.hostname.lower()
    if host != "localhost":
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ModelInvocationError("Ollama endpoint host must be loopback") from exc
        if not address.is_loopback:
            raise ModelInvocationError("Ollama endpoint host must be loopback")
    return endpoint


def _digest(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OllamaChatConfig:
    """Explicit non-secret configuration for one local Ollama chat adapter."""

    endpoint: str
    model_id: str
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        _validate_loopback_chat_endpoint(self.endpoint)
        _canonical_text("model_id", self.model_id)
        if (
            type(self.max_response_bytes) is not int
            or not _MIN_RESPONSE_BYTES <= self.max_response_bytes <= _MAX_RESPONSE_BYTES
        ):
            raise ModelInvocationError(
                f"max_response_bytes must be integer {_MIN_RESPONSE_BYTES}..{_MAX_RESPONSE_BYTES}"
            )

    @property
    def config_sha256(self) -> str:
        return _digest(
            {
                "endpoint": self.endpoint,
                "model_id": self.model_id,
                "max_response_bytes": self.max_response_bytes,
                "api": "chat",
                "stream": False,
                "think": False,
            }
        )


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _http_post_json(
    endpoint: str,
    payload: bytes,
    *,
    timeout_seconds: float,
    max_response_bytes: int,
) -> bytes:
    request = Request(
        endpoint,
        data=payload,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    opener = build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", None)
            if status != 200:
                raise ConnectionError("Ollama endpoint unavailable")
            body = response.read(max_response_bytes + 1)
    except HTTPError as exc:
        if exc.code in (408, 504):
            raise TimeoutError("Ollama request timed out") from None
        raise ConnectionError("Ollama endpoint unavailable") from None
    except (TimeoutError, socket.timeout):
        raise TimeoutError("Ollama request timed out") from None
    except URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise TimeoutError("Ollama request timed out") from None
        raise ConnectionError("Ollama endpoint unavailable") from None
    except OSError:
        raise ConnectionError("Ollama endpoint unavailable") from None

    if len(body) > max_response_bytes:
        raise ModelAdapterInvalidResponse("Ollama response exceeds configured size limit")
    return body


class OllamaModelAdapter:
    """Translate canonical model requests to one local Ollama chat endpoint."""

    def __init__(self, config: OllamaChatConfig) -> None:
        if type(config) is not OllamaChatConfig:
            raise ModelInvocationError("config must be OllamaChatConfig")
        self._config = config
        self.descriptor = ModelAdapterDescriptor(
            mode=ModelBackendMode.LOCAL_OLLAMA,
            backend_id=_BACKEND_ID,
            endpoint_class=_ENDPOINT_CLASS,
            model_id=config.model_id,
            config_sha256=config.config_sha256,
        )

    @property
    def config(self) -> OllamaChatConfig:
        return self._config

    def invoke(
        self,
        request: ModelInvocationRequest,
        *,
        timeout_seconds: Decimal,
    ) -> ModelAdapterResponse:
        if type(request) is not ModelInvocationRequest:
            raise ModelInvocationError("request must be ModelInvocationRequest")
        if (
            type(timeout_seconds) is not Decimal
            or not timeout_seconds.is_finite()
            or timeout_seconds <= 0
        ):
            raise ModelInvocationError("timeout_seconds must be a positive finite Decimal")

        transport_timeout = float(timeout_seconds)
        if not math.isfinite(transport_timeout) or transport_timeout <= 0:
            raise TimeoutError("Ollama timeout is outside transport range")

        payload = json.dumps(
            {
                "model": self._config.model_id,
                "messages": [{"role": "user", "content": request.input_text}],
                "stream": False,
                "think": False,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

        raw = _http_post_json(
            self._config.endpoint,
            payload,
            timeout_seconds=transport_timeout,
            max_response_bytes=self._config.max_response_bytes,
        )
        try:
            decoded = raw.decode("utf-8", errors="strict")
            parsed = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelAdapterInvalidResponse("Ollama response is not valid UTF-8 JSON") from exc

        if type(parsed) is not dict:
            raise ModelAdapterInvalidResponse("Ollama response must be a JSON object")
        if "error" in parsed:
            raise ModelAdapterInvalidResponse("Ollama response reports an error")
        if parsed.get("model") != self._config.model_id:
            raise ModelAdapterInvalidResponse("Ollama response model identity mismatch")
        if parsed.get("done") is not True:
            raise ModelAdapterInvalidResponse("Ollama non-streaming response is incomplete")

        message = parsed.get("message")
        if type(message) is not dict or message.get("role") != "assistant":
            raise ModelAdapterInvalidResponse("Ollama assistant message is invalid")
        content = message.get("content")
        if type(content) is not str or not content or not content.strip():
            raise ModelAdapterInvalidResponse("Ollama assistant content is invalid")
        try:
            content.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ModelAdapterInvalidResponse("Ollama assistant content is invalid UTF-8") from exc

        return ModelAdapterResponse(text=content, model_id=self._config.model_id)
