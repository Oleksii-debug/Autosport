from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_NORMALIZED_KEYS = frozenset(
    {
        "apikey",
        "xapikey",
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "authtoken",
        "bearertoken",
        "password",
        "passwd",
        "pwd",
        "secret",
        "clientsecret",
        "authorization",
        "credential",
        "credentials",
    }
)
_SENSITIVE_SUFFIXES = (
    "apikey",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "authtoken",
    "bearertoken",
    "password",
    "passwd",
    "pwd",
    "secret",
    "authorization",
    "credential",
    "credentials",
)

_URL_USERINFO_RE = re.compile(
    r"(?i)\b(?P<scheme>(?:https?|wss?)://)(?P<userinfo>[^/@\s]+)@"
)
_QUERY_PARAM_RE = re.compile(
    r"(?P<prefix>[?&](?P<key>[^=&#\s]+)=)(?P<value>[^&#\s]*)"
)
_BEARER_RE = re.compile(
    r"(?i)\b(?P<scheme>bearer)\s+(?P<value>[A-Za-z0-9._~+/=-]{4,})"
)
_KEY_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>(?P<key>[A-Za-z0-9_.-]+)\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&}\]]+)"
)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = _normalized_key(key)
    if not normalized:
        return False
    return normalized in _SENSITIVE_NORMALIZED_KEYS or normalized.endswith(
        _SENSITIVE_SUFFIXES
    )


def _environment_secret_values() -> tuple[str, ...]:
    values: set[str] = set()
    for name, value in os.environ.items():
        if not is_sensitive_key(name) or not value:
            continue
        # Very short bare values cannot be distinguished safely from ordinary prose.
        # Key-aware patterns below still redact short secrets when they are labelled.
        if len(value) >= 4:
            values.add(value)
    return tuple(sorted(values, key=lambda item: (-len(item), item)))


def _secret_values(extra_secret_values: Iterable[str]) -> tuple[str, ...]:
    values = set(_environment_secret_values())
    for value in extra_secret_values:
        if isinstance(value, str) and len(value) >= 4:
            values.add(value)
    return tuple(sorted(values, key=lambda item: (-len(item), item)))


def _redacted_value_literal(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[0] + REDACTED + value[-1]
    return REDACTED


def redact_operator_text(
    text: str,
    *,
    extra_secret_values: Iterable[str] = (),
) -> str:
    """Redact credential material before product-owned operator presentation.

    The function is deliberately presentation-only. It never mutates provider
    requests, authentication state, durable economic truth, or configuration.
    """

    if not isinstance(text, str):
        raise TypeError("text must be str")
    rendered = str.__str__(text)

    for secret in _secret_values(extra_secret_values):
        rendered = rendered.replace(secret, REDACTED)

    rendered = _URL_USERINFO_RE.sub(
        lambda match: match.group("scheme") + REDACTED + "@",
        rendered,
    )

    def redact_query(match: re.Match[str]) -> str:
        if not is_sensitive_key(match.group("key")):
            return match.group(0)
        return match.group("prefix") + REDACTED

    rendered = _QUERY_PARAM_RE.sub(redact_query, rendered)
    rendered = _BEARER_RE.sub(
        lambda match: match.group("scheme") + " " + REDACTED,
        rendered,
    )

    def redact_key_value(match: re.Match[str]) -> str:
        if not is_sensitive_key(match.group("key")):
            return match.group(0)
        return match.group("prefix") + _redacted_value_literal(match.group("value"))

    return _KEY_VALUE_RE.sub(redact_key_value, rendered)


def redact_operator_value(
    value: Any,
    *,
    extra_secret_values: Iterable[str] = (),
) -> Any:
    """Return a redacted presentation copy of nested operator data."""

    secrets = tuple(extra_secret_values)

    if isinstance(value, str):
        return redact_operator_text(value, extra_secret_values=secrets)
    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                redacted[key] = REDACTED
            else:
                redacted[key] = redact_operator_value(
                    item,
                    extra_secret_values=secrets,
                )
        return redacted
    if isinstance(value, list):
        return [
            redact_operator_value(item, extra_secret_values=secrets)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            redact_operator_value(item, extra_secret_values=secrets)
            for item in value
        )
    return value


def safe_exception_detail(
    exc: BaseException,
    *,
    unavailable_detail: str = "exception details unavailable",
    extra_secret_values: Iterable[str] = (),
) -> str:
    """Render and redact exception detail without trusting hostile __str__ output."""

    try:
        detail = str.__str__(str(exc))
    except BaseException:
        detail = unavailable_detail
    return redact_operator_text(
        detail,
        extra_secret_values=extra_secret_values,
    )


def safe_exception_text(
    exc: BaseException,
    *,
    unavailable_detail: str = "exception details unavailable",
    extra_secret_values: Iterable[str] = (),
) -> str:
    """Render a safe Type: detail string and redact secret material."""

    try:
        exception_type = str.__str__(type.__getattribute__(type(exc), "__name__"))
    except BaseException:
        exception_type = "BaseException"

    detail = safe_exception_detail(
        exc,
        unavailable_detail=unavailable_detail,
        extra_secret_values=extra_secret_values,
    )
    if not detail:
        return exception_type
    return exception_type + ": " + detail
