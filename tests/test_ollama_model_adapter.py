from __future__ import annotations

import json
from decimal import Decimal
from urllib.error import URLError
from urllib.request import ProxyHandler

import pytest

import autosport.ollama_model_adapter as module
from autosport.model_invocation import (
    ModelAdapterInvalidResponse,
    ModelBackendMode,
    ModelInvocationError,
    ModelInvocationPolicy,
    ModelInvocationRequest,
    ModelInvocationStatus,
    invoke_optional_model,
)
from autosport.ollama_model_adapter import OllamaChatConfig, OllamaModelAdapter


ENDPOINT = "http://localhost:11434/api/chat"
MODEL = "qwen3:8b"


def request(**overrides):
    values = {
        "invocation_id": "ollama-1",
        "capability": "optional-explanation",
        "input_text": "Поясни стан ринку",
        "deadline_seconds": Decimal("3"),
        "idempotent": True,
    }
    values.update(overrides)
    return ModelInvocationRequest(**values)


def response_bytes(*, model=MODEL, content="Локальна відповідь", done=True, role="assistant"):
    return json.dumps(
        {"model": model, "message": {"role": role, "content": content}, "done": done},
        ensure_ascii=False,
    ).encode("utf-8")


def test_config_is_loopback_only_and_hash_binds_effective_wire_contract():
    one = OllamaChatConfig(ENDPOINT, MODEL)
    same = OllamaChatConfig(ENDPOINT, MODEL)
    changed = OllamaChatConfig(ENDPOINT, "qwen3:14b")
    assert one.config_sha256 == same.config_sha256
    assert one.config_sha256 != changed.config_sha256

    for invalid in (
        "https://localhost:11434/api/chat",
        "http://example.com:11434/api/chat",
        "http://localhost:11434/api/generate",
        "http://user:secret@localhost:11434/api/chat",
        "http://localhost:11434/api/chat?token=secret",
        "http://localhost:11434/api/chat#fragment",
    ):
        with pytest.raises(ModelInvocationError):
            OllamaChatConfig(invalid, MODEL)


def test_ipv4_and_ipv6_loopback_are_allowed_but_non_loopback_ip_is_rejected():
    OllamaChatConfig("http://127.0.0.1:11434/api/chat", MODEL)
    OllamaChatConfig("http://[::1]:11434/api/chat", MODEL)
    with pytest.raises(ModelInvocationError, match="loopback"):
        OllamaChatConfig("http://192.168.1.10:11434/api/chat", MODEL)


def test_adapter_descriptor_is_exact_local_identity():
    config = OllamaChatConfig(ENDPOINT, MODEL)
    adapter = OllamaModelAdapter(config)
    assert adapter.descriptor.mode is ModelBackendMode.LOCAL_OLLAMA
    assert adapter.descriptor.backend_id == "ollama-local-http"
    assert adapter.descriptor.endpoint_class == "LOCAL_LOOPBACK_HTTP"
    assert adapter.descriptor.model_id == MODEL
    assert adapter.descriptor.config_sha256 == config.config_sha256


def test_wire_payload_forces_chat_nonstreaming_no_thinking(monkeypatch):
    captured = {}

    def fake(endpoint, payload, *, timeout_seconds, max_response_bytes):
        captured.update(
            endpoint=endpoint,
            payload=json.loads(payload.decode("utf-8")),
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
        return response_bytes()

    monkeypatch.setattr(module, "_http_post_json", fake)
    adapter = OllamaModelAdapter(OllamaChatConfig(ENDPOINT, MODEL))
    result = adapter.invoke(request(), timeout_seconds=Decimal("1.25"))

    assert captured == {
        "endpoint": ENDPOINT,
        "payload": {
            "messages": [{"content": "Поясни стан ринку", "role": "user"}],
            "model": MODEL,
            "stream": False,
            "think": False,
        },
        "timeout_seconds": 1.25,
        "max_response_bytes": 1_048_576,
    }
    assert result.text == "Локальна відповідь"
    assert result.model_id == MODEL


def test_invalid_response_shapes_fail_closed(monkeypatch):
    adapter = OllamaModelAdapter(OllamaChatConfig(ENDPOINT, MODEL))
    invalid = [
        b"not-json",
        b"[]",
        json.dumps({"error": "token=SECRET"}).encode(),
        response_bytes(model="other"),
        response_bytes(done=False),
        response_bytes(role="user"),
        response_bytes(content="   "),
    ]
    for body in invalid:
        monkeypatch.setattr(module, "_http_post_json", lambda *a, _body=body, **k: _body)
        with pytest.raises(ModelAdapterInvalidResponse):
            adapter.invoke(request(), timeout_seconds=Decimal("1"))


def test_parent_contract_normalizes_adapter_success_without_persisting_prompt(monkeypatch):
    monkeypatch.setattr(module, "_http_post_json", lambda *a, **k: response_bytes())
    adapter = OllamaModelAdapter(OllamaChatConfig(ENDPOINT, MODEL))
    completion = invoke_optional_model(
        request(),
        ModelInvocationPolicy(mode=ModelBackendMode.LOCAL_OLLAMA),
        {ModelBackendMode.LOCAL_OLLAMA: adapter},
    )
    assert completion.result.status is ModelInvocationStatus.SUCCESS
    assert completion.response_text == "Локальна відповідь"
    evidence = json.dumps(completion.result.payload(), ensure_ascii=False)
    assert "Поясни стан ринку" not in evidence
    assert ENDPOINT not in evidence


def test_parent_contract_normalizes_timeout_and_unavailable_without_secret_detail(monkeypatch):
    adapter = OllamaModelAdapter(OllamaChatConfig(ENDPOINT, MODEL))

    def timed_out(*args, **kwargs):
        raise TimeoutError("Bearer SECRET")

    monkeypatch.setattr(module, "_http_post_json", timed_out)
    timeout_completion = invoke_optional_model(
        request(),
        ModelInvocationPolicy(mode=ModelBackendMode.LOCAL_OLLAMA),
        {ModelBackendMode.LOCAL_OLLAMA: adapter},
    )
    assert timeout_completion.result.status is ModelInvocationStatus.TIMEOUT
    assert "SECRET" not in json.dumps(timeout_completion.result.payload())

    def unavailable(*args, **kwargs):
        raise ConnectionError("api_key=SECRET")

    monkeypatch.setattr(module, "_http_post_json", unavailable)
    unavailable_completion = invoke_optional_model(
        request(),
        ModelInvocationPolicy(mode=ModelBackendMode.LOCAL_OLLAMA),
        {ModelBackendMode.LOCAL_OLLAMA: adapter},
    )
    assert unavailable_completion.result.status is ModelInvocationStatus.UNAVAILABLE
    assert "SECRET" not in json.dumps(unavailable_completion.result.payload())


def test_transport_disables_environment_proxies(monkeypatch):
    captured = {}

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, size):
            return b"{}"

    class Opener:
        def open(self, request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(module, "build_opener", fake_build_opener)
    module._http_post_json(ENDPOINT, b"{}", timeout_seconds=1.5, max_response_bytes=1024)
    proxy_handlers = [handler for handler in captured["handlers"] if isinstance(handler, ProxyHandler)]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}
    assert captured["request"].get_header("Authorization") is None
    assert captured["request"].full_url == ENDPOINT
    assert captured["timeout"] == 1.5


def test_transport_helper_maps_url_errors_without_leaking_reason(monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            raise URLError("token=SUPERSECRET")

    monkeypatch.setattr(module, "build_opener", lambda *args: Opener())
    with pytest.raises(ConnectionError) as caught:
        module._http_post_json(
            ENDPOINT,
            b"{}",
            timeout_seconds=1.0,
            max_response_bytes=1024,
        )
    assert "SUPERSECRET" not in str(caught.value)


def test_oversized_response_is_rejected_before_json_parse(monkeypatch):
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, size):
            return b"x" * size

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(module, "build_opener", lambda *args: Opener())
    with pytest.raises(ModelAdapterInvalidResponse, match="size limit"):
        module._http_post_json(
            ENDPOINT,
            b"{}",
            timeout_seconds=1.0,
            max_response_bytes=1024,
        )


def test_adapter_rejects_wrong_types_and_non_transportable_timeout():
    adapter = OllamaModelAdapter(OllamaChatConfig(ENDPOINT, MODEL))
    with pytest.raises(ModelInvocationError, match="OllamaChatConfig"):
        OllamaModelAdapter(object())
    with pytest.raises(ModelInvocationError, match="request"):
        adapter.invoke(object(), timeout_seconds=Decimal("1"))
    with pytest.raises(ModelInvocationError, match="positive finite"):
        adapter.invoke(request(), timeout_seconds=Decimal("0"))
    with pytest.raises(TimeoutError, match="transport range"):
        adapter.invoke(request(), timeout_seconds=Decimal("1e10000"))
