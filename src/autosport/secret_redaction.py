from __future__ import annotations

import base64
import builtins
import os
import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus, unquote_plus
from typing import Any

REDACTED = "[REDACTED]"

# Structured Mapping keys are presentation input. Bound recursive composite-key
# sanitization well below Python recursion limits and cap total key nodes so a
# hostile diagnostic payload cannot turn redaction itself into an availability sink.
_MAPPING_KEY_MAX_TUPLE_DEPTH = 32
_MAPPING_KEY_MAX_NODES = 256
_OPERATOR_VALUE_MAX_DEPTH = 64
_OPERATOR_VALUE_MAX_NODES = 10_000
_QUERY_KEY_MAX_DECODE_PASSES = 8
_SECRET_VALUE_MAX_URL_ENCODING_PASSES = 8
_SECRET_VALUE_MAX_BASE64_ENCODING_PASSES = 4
_SECRET_VALUE_MAX_MIXED_ENCODING_TRANSITIONS = 2
_SECRET_VALUE_MAX_MIXED_VARIANTS_PER_SECRET = 384

_SENSITIVE_NORMALIZED_KEYS = frozenset(
    {
        "apikey",
        "xapikey",
        "appkey",
        "applicationkey",
        "authentication",
        "xapplication",
        "xauthentication",
        "sessiontoken",
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
        "secretaccesskey",
        "privatekey",
        "authorization",
        "credential",
        "credentials",
        "cookie",
        "setcookie",
    }
)
_SENSITIVE_SUFFIXES = (
    "apikey",
    "appkey",
    "applicationkey",
    "authentication",
    "sessiontoken",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "authtoken",
    "bearertoken",
    "password",
    "passwd",
    "pwd",
    "secret",
    "secretaccesskey",
    "privatekey",
    "authorization",
    "credential",
    "credentials",
)
_SENSITIVE_WRAPPER_SUFFIXES = ("value", "header")

_URL_USERINFO_RE = re.compile(
    r"\b(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^/@\s]+)@"
)
_QUERY_PARAM_RE = re.compile(
    r"(?P<prefix>[?&](?P<key>[^=&#\s]+)=)(?P<value>[^&#\s]*)"
)
_AUTHORIZATION_COMMA_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>\bauthorization\s*[:=]\s*)"
    r"(?P<value>[A-Za-z][A-Za-z0-9+.-]*\s+[^\r\n]*,[^\r\n]*)"
)
_AUTHORIZATION_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>\bauthorization\s*[:=]\s*)"
    r"(?P<value>\[REDACTED\]|\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|"
    r"[A-Za-z][A-Za-z0-9+.-]*\s+[^\s,;&}\]]+|[^\s,;&}\]]+)"
)
_BEARER_RE = re.compile(
    r"(?i)\b(?P<scheme>bearer)\s+(?P<value>[A-Za-z0-9._~+/=-]{4,})"
)
_KEY_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>(?:"
    r"\"(?P<double_key>(?:\\.|[^\"\\\r\n])*)\"|"
    r"'(?P<single_key>(?:\\.|[^'\\\r\n])*)'|"
    r"(?P<bare_key>[A-Za-z0-9_.\\-]+)"
    r")\s*[:=]\s*)"
    r"(?P<value>\[REDACTED\]|\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|[^\s,;&}\]]+)"
)
_OVERLAPPING_KEY_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>(?:"
    r"\"(?P<double_key>(?:\\.|[^\"\\\r\n])*)\"|"
    r"'(?P<single_key>(?:\\.|[^'\\\r\n])*)'|"
    r"(?P<bare_key>[A-Za-z0-9_.\\-]+)"
    r")\s*[:=]\s*)"
    r"(?P<value>\[REDACTED\]|\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|[^\s,;&}\]\"')]+)"
)
_COOKIE_HEADER_RE = re.compile(
    r"(?i)(?P<prefix>(?<![A-Za-z0-9_.-])(?:set-cookie|cookie)\s*:\s*)"
    r"(?P<value>[^\r\n]*)"
)

_SPACED_SENSITIVE_KEY_VALUE_RE = re.compile(
    r"(?i)(?P<prefix>\b(?P<key>"
    r"(?:x[ \t]+)?api[ \t]+key|"
    r"application[ \t]+key|x[ \t]+application|x[ \t]+authentication|"
    r"session[ \t]+token|access[ \t]+token|refresh[ \t]+token|"
    r"id[ \t]+token|auth[ \t]+token|bearer[ \t]+token|"
    r"client[ \t]+secret|secret[ \t]+access[ \t]+key|private[ \t]+key"
    r")\s*[:=]\s*)"
    r"(?P<value>\[REDACTED\]|\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|[^\s,;&}\]]+)"
)
_KEY_ESCAPE_RE = re.compile(
    r"\\(?:(?P<u16>u[0-9A-Fa-f]{4})|"
    r"(?P<u32>U[0-9A-Fa-f]{8})|(?P<x8>x[0-9A-Fa-f]{2}))"
)
_KEY_SIMPLE_ESCAPE_RE = re.compile(r"\\(?P<simple>[abfnrtv])")
_KEY_SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}
_KEY_OCTAL_ESCAPE_RE = re.compile(r"\\(?P<octal>[0-7]{1,3})")


def _decode_escaped_key_for_classification(value: str) -> str:
    """Decode bounded escapes only for sensitive-key classification.

    Operator text keeps its original spelling. This closes serialized JSON/
    Python-repr credential-key bypasses without interpreting arbitrary values.
    """

    def replace(match: re.Match[str]) -> str:
        token = match.group("u16") or match.group("u32") or match.group("x8")
        try:
            return chr(int(token[1:], 16))
        except ValueError:
            return match.group(0)

    decoded = _KEY_ESCAPE_RE.sub(replace, value)
    decoded = _KEY_SIMPLE_ESCAPE_RE.sub(
        lambda match: _KEY_SIMPLE_ESCAPES[match.group("simple")],
        decoded,
    )
    return _KEY_OCTAL_ESCAPE_RE.sub(
        lambda match: chr(int(match.group("octal"), 8)),
        decoded,
    )


def _normalized_key(value: str) -> str:
    decoded = _decode_escaped_key_for_classification(value)
    return re.sub(r"[^a-z0-9]", "", decoded.casefold())


def is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = _normalized_key(key)
    if not normalized:
        return False

    # Provider/config payloads frequently decorate credential names with
    # presentation/container suffixes such as Value/Header. Classify at most
    # two bounded wrappers without broad substring matching, so ordinary keys
    # (for example market_value) stay non-sensitive while appKeyValue and
    # betfairAppKeyHeader retain the underlying credential identity.
    candidates = [normalized]
    candidate = normalized
    for _ in range(2):
        stripped = None
        for wrapper in _SENSITIVE_WRAPPER_SUFFIXES:
            if candidate.endswith(wrapper) and len(candidate) > len(wrapper):
                stripped = candidate[: -len(wrapper)]
                break
        if stripped is None:
            break
        candidate = stripped
        candidates.append(candidate)

    return any(
        item in _SENSITIVE_NORMALIZED_KEYS or item.endswith(_SENSITIVE_SUFFIXES)
        for item in candidates
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


def _reversible_base64_secret_values(secrets: Iterable[str]) -> tuple[str, ...]:
    """Return bounded nested Base64 spellings of already-known secret values."""

    values: set[str] = set()
    for secret in secrets:
        frontier = {secret}
        for _ in range(_SECRET_VALUE_MAX_BASE64_ENCODING_PASSES):
            next_frontier: set[str] = set()
            for candidate in frontier:
                raw = candidate.encode("utf-8")
                standard = base64.b64encode(raw).decode("ascii")
                urlsafe = base64.urlsafe_b64encode(raw).decode("ascii")
                for encoded in (
                    standard,
                    standard.rstrip("="),
                    urlsafe,
                    urlsafe.rstrip("="),
                ):
                    if encoded and encoded != secret and encoded not in values:
                        values.add(encoded)
                        next_frontier.add(encoded)
            if not next_frontier:
                break
            frontier = next_frontier
    return tuple(sorted(values, key=lambda item: (-len(item), item)))


def _reversible_url_secret_values(secrets: Iterable[str]) -> tuple[str, ...]:
    """Return bounded nested URL spellings of already-known secret values.

    Provider, proxy and logging layers can percent-encode an already encoded value.
    Those forms remain trivially reversible credential material.  Derive them only
    from secrets already authoritative for this call; never decode arbitrary text.
    """

    values: set[str] = set()
    for secret in secrets:
        frontier = {secret}
        for _ in range(_SECRET_VALUE_MAX_URL_ENCODING_PASSES):
            next_frontier: set[str] = set()
            for candidate in frontier:
                for encoded in (
                    quote(candidate, safe=""),
                    quote_plus(candidate, safe=""),
                ):
                    if encoded != secret and encoded not in values:
                        values.add(encoded)
                        next_frontier.add(encoded)
            if not next_frontier:
                break
            frontier = next_frontier
    values.discard("")
    return tuple(sorted(values, key=lambda item: (-len(item), item)))


def _normalize_percent_escape_case(value: str) -> str:
    """Canonicalize only percent-hex case while preserving literal text case."""

    return re.sub(
        r"%[0-9A-Fa-f]{2}",
        lambda match: "%" + match.group(0)[1:].upper(),
        value,
    )


def _replace_url_encoded_secret(text: str, encoded_secret: str) -> str:
    """Redact one encoded secret with percent-hex case-insensitive matching."""

    normalized_secret = _normalize_percent_escape_case(encoded_secret)
    rendered = text
    search_from = 0
    while True:
        normalized_rendered = _normalize_percent_escape_case(rendered)
        index = normalized_rendered.find(normalized_secret, search_from)
        if index < 0:
            return rendered
        rendered = (
            rendered[:index]
            + REDACTED
            + rendered[index + len(encoded_secret) :]
        )
        search_from = index + len(REDACTED)


def _mixed_reversible_secret_values(
    secrets: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return bounded alternating compositions of known-secret encodings.

    Existing URL and Base64 closures cover repeated transforms within one family.
    Provider/logging stacks can alternate families, so start from those bounded
    closures and permit only a small number of cross-family transitions. A strict
    per-secret state budget prevents adversarial configuration from turning
    presentation redaction into unbounded combinatorial work.

    The first tuple contains exact/case-sensitive Base64 spellings. The second
    contains URL spellings and therefore uses percent-hex case-insensitive matching
    at publication time.
    """

    mixed_base64: set[str] = set()
    mixed_url: set[str] = set()

    for secret in secrets:
        base64_family = _reversible_base64_secret_values((secret,))
        url_family = _reversible_url_secret_values((secret,))
        frontier: list[tuple[str, str]] = [
            *(("base64", value) for value in base64_family),
            *(("url", value) for value in url_family),
        ]
        seen_states = set(frontier)

        for _ in range(_SECRET_VALUE_MAX_MIXED_ENCODING_TRANSITIONS):
            next_frontier: list[tuple[str, str]] = []
            for family, candidate in sorted(frontier):
                if len(seen_states) >= _SECRET_VALUE_MAX_MIXED_VARIANTS_PER_SECRET:
                    break

                if family == "base64":
                    variants = (
                        quote(candidate, safe=""),
                        quote_plus(candidate, safe=""),
                    )
                    destination_family = "url"
                else:
                    raw = candidate.encode("utf-8")
                    standard = base64.b64encode(raw).decode("ascii")
                    urlsafe = base64.urlsafe_b64encode(raw).decode("ascii")
                    variants = (
                        standard,
                        standard.rstrip("="),
                        urlsafe,
                        urlsafe.rstrip("="),
                    )
                    destination_family = "base64"

                for encoded in variants:
                    if not encoded or encoded == candidate:
                        continue
                    if destination_family == "base64":
                        mixed_base64.add(encoded)
                    else:
                        mixed_url.add(encoded)
                    state = (destination_family, encoded)
                    if state in seen_states:
                        continue
                    if len(seen_states) >= _SECRET_VALUE_MAX_MIXED_VARIANTS_PER_SECRET:
                        break
                    seen_states.add(state)
                    next_frontier.append(state)

            if not next_frontier:
                break
            frontier = next_frontier

    return (
        tuple(sorted(mixed_base64, key=lambda item: (-len(item), item))),
        tuple(sorted(mixed_url, key=lambda item: (-len(item), item))),
    )

def _decode_query_key_for_classification(value: str) -> tuple[str, bool]:
    """Return a bounded decoded query key plus unresolved-nesting truth."""

    decoded = value
    for _ in range(_QUERY_KEY_MAX_DECODE_PASSES):
        next_decoded = unquote_plus(decoded)
        if next_decoded == decoded:
            return decoded, False
        decoded = next_decoded

    # A key that is still changing after the bounded decode budget is not safe
    # to classify as ordinary. Callers must fail closed for its associated value.
    return decoded, unquote_plus(decoded) != decoded


def _redacted_value_literal(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[0] + REDACTED + value[-1]
    return REDACTED


def _key_value_match_key(match: re.Match[str]) -> str:
    key = match.group("double_key")
    if key is None:
        key = match.group("single_key")
    if key is None:
        key = match.group("bare_key")
    return key


def _redact_overlapping_sensitive_key_values(text: str) -> str:
    """Redact inner credential pairs even when an outer safe pair spans them."""

    rendered = text
    search_from = 0
    while True:
        match = _OVERLAPPING_KEY_VALUE_RE.search(rendered, search_from)
        if match is None:
            return rendered
        if not is_sensitive_key(_key_value_match_key(match)):
            # Advance from the candidate start, not its end. A non-sensitive
            # wrapper such as detail="api_key=..." may contain a sensitive pair.
            search_from = match.start() + 1
            continue
        replacement = match.group("prefix") + _redacted_value_literal(
            match.group("value")
        )
        rendered = rendered[: match.start()] + replacement + rendered[match.end() :]
        search_from = match.start() + len(replacement)


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

    secrets = _secret_values(extra_secret_values)
    if secrets:
        # A real configured secret may itself contain the placeholder literal.
        # Redact those exact values before protecting already-redacted output.
        marker_secrets = tuple(secret for secret in secrets if REDACTED in secret)
        for secret in marker_secrets:
            rendered = rendered.replace(secret, REDACTED)

        # Never re-redact the placeholder itself, even when another configured
        # secret happens to be a substring of the literal REDACTED marker.
        ordinary_secrets = tuple(secret for secret in secrets if REDACTED not in secret)
        parts = rendered.split(REDACTED)
        for index, part in enumerate(parts):
            for secret in ordinary_secrets:
                part = part.replace(secret, REDACTED)
            parts[index] = part
        rendered = REDACTED.join(parts)

        # Base64 and URL percent-encoding are reversible credential material,
        # not redaction. Derive only spellings of secrets already authoritative
        # for this call; never decode/classify arbitrary opaque values.
        reversible_base64_secrets = _reversible_base64_secret_values(secrets)
        reversible_url_secrets = _reversible_url_secret_values(secrets)
        (
            mixed_base64_secrets,
            mixed_url_secrets,
        ) = _mixed_reversible_secret_values(secrets)
        if (
            reversible_base64_secrets
            or reversible_url_secrets
            or mixed_base64_secrets
            or mixed_url_secrets
        ):
            parts = rendered.split(REDACTED)
            for index, part in enumerate(parts):
                for encoded_secret in (
                    *reversible_base64_secrets,
                    *mixed_base64_secrets,
                ):
                    part = part.replace(encoded_secret, REDACTED)
                for encoded_secret in (
                    *reversible_url_secrets,
                    *mixed_url_secrets,
                ):
                    part = _replace_url_encoded_secret(part, encoded_secret)
                parts[index] = part
            rendered = REDACTED.join(parts)

    rendered = _URL_USERINFO_RE.sub(
        lambda match: match.group("scheme") + REDACTED + "@",
        rendered,
    )

    def redact_query(match: re.Match[str]) -> str:
        decoded_key, unresolved_nested_encoding = (
            _decode_query_key_for_classification(match.group("key"))
        )
        if not unresolved_nested_encoding and not is_sensitive_key(decoded_key):
            return match.group(0)
        return match.group("prefix") + REDACTED

    rendered = _QUERY_PARAM_RE.sub(redact_query, rendered)
    rendered = _COOKIE_HEADER_RE.sub(
        lambda match: match.group("prefix") + REDACTED,
        rendered,
    )
    # Multi-parameter Authorization schemes (for example Digest and AWS SigV4)
    # carry credential material after comma-separated fields. Redact the entire
    # header value through the current line before the narrower single-token
    # rule runs, otherwise response/signature tails can survive presentation.
    rendered = _AUTHORIZATION_COMMA_VALUE_RE.sub(
        lambda match: match.group("prefix") + REDACTED,
        rendered,
    )
    rendered = _AUTHORIZATION_VALUE_RE.sub(
        lambda match: match.group("prefix")
        + _redacted_value_literal(match.group("value")),
        rendered,
    )
    rendered = _BEARER_RE.sub(
        lambda match: match.group("scheme") + " " + REDACTED,
        rendered,
    )

    def redact_spaced_key_value(match: re.Match[str]) -> str:
        if not is_sensitive_key(match.group("key")):
            return match.group(0)
        return match.group("prefix") + _redacted_value_literal(match.group("value"))

    rendered = _SPACED_SENSITIVE_KEY_VALUE_RE.sub(
        redact_spaced_key_value,
        rendered,
    )

    rendered = _redact_overlapping_sensitive_key_values(rendered)

    def redact_key_value(match: re.Match[str]) -> str:
        if not is_sensitive_key(_key_value_match_key(match)):
            return match.group(0)
        return match.group("prefix") + _redacted_value_literal(match.group("value"))

    return _KEY_VALUE_RE.sub(redact_key_value, rendered)


def _redact_operator_mapping_key(
    key: object,
    *,
    secrets: tuple[str, ...],
    _depth: int = 0,
    _remaining_nodes: list[int] | None = None,
    _global_remaining_nodes: list[int] | None = None,
) -> tuple[object, bool, bool]:
    """Return one safe hashable presentation key plus sensitivity/transform flags.

    Only exact built-in key domains are trusted for structural preservation. Unknown
    hashable classes are presentation input too; never call their __str__/__repr__ or
    publish them unchanged because either surface may carry credential material.

    Tuple recursion is explicitly bounded. Exhausting either the depth or total-node
    budget fails closed to the ordinary redaction marker and makes the associated value
    sensitive as well.
    """

    if _remaining_nodes is None:
        _remaining_nodes = [_MAPPING_KEY_MAX_NODES]
    if _depth > _MAPPING_KEY_MAX_TUPLE_DEPTH or _remaining_nodes[0] <= 0:
        return REDACTED, True, True
    if _global_remaining_nodes is not None:
        if _global_remaining_nodes[0] <= 0:
            return REDACTED, True, True
        _global_remaining_nodes[0] -= 1
    _remaining_nodes[0] -= 1

    if type(key) is str:
        safe_key = redact_operator_text(key, extra_secret_values=secrets)
        return safe_key, is_sensitive_key(key), safe_key != key

    if type(key) is bytes:
        try:
            decoded_key = bytes.decode(key, "utf-8", "strict")
        except UnicodeDecodeError:
            # Undecodable structured keys cannot be classified safely. Redact both
            # the presentation key and its associated value.
            return REDACTED, True, True
        safe_key = redact_operator_text(
            decoded_key,
            extra_secret_values=secrets,
        )
        return safe_key, is_sensitive_key(decoded_key), True

    if type(key) is tuple:
        if (
            _depth >= _MAPPING_KEY_MAX_TUPLE_DEPTH
            or len(key) > _remaining_nodes[0]
        ):
            return REDACTED, True, True
        safe_parts: list[object] = []
        key_is_sensitive = False
        key_was_transformed = False
        for part in key:
            safe_part, part_is_sensitive, part_was_transformed = (
                _redact_operator_mapping_key(
                    part,
                    secrets=secrets,
                    _depth=_depth + 1,
                    _remaining_nodes=_remaining_nodes,
                    _global_remaining_nodes=_global_remaining_nodes,
                )
            )
            safe_parts.append(safe_part)
            key_is_sensitive = key_is_sensitive or part_is_sensitive
            key_was_transformed = key_was_transformed or part_was_transformed
        return tuple(safe_parts), key_is_sensitive, key_was_transformed

    # These exact scalar built-ins cannot carry hidden string/object presentation
    # state. Preserve them so ordinary numeric/boolean/None structured keys remain
    # stable. Complex is included because its exact built-in representation contains
    # only numeric components.
    if key is None or type(key) in (bool, int, float, complex):
        return key, False, False

    # Unknown hashable key classes (including str/bytes subclasses and containers
    # such as frozenset) are not presentation authority. Fail closed without invoking
    # arbitrary conversion methods and redact the associated value as well.
    return REDACTED, True, True


def _mapping_key_collision_alias(safe_key: object, suffix: int) -> object:
    """Return a deterministic safe alias without rendering composite key objects."""

    if type(safe_key) is str:
        return f"{safe_key}#{suffix}"
    return (safe_key, suffix)


def redact_operator_value(
    value: Any,
    *,
    extra_secret_values: Iterable[str] = (),
) -> Any:
    """Return a bounded redacted presentation copy of nested operator data."""

    secrets = tuple(extra_secret_values)
    remaining_nodes = [_OPERATOR_VALUE_MAX_NODES]
    active_container_ids: set[int] = set()

    def redact(item: Any, *, depth: int) -> Any:
        if depth > _OPERATOR_VALUE_MAX_DEPTH or remaining_nodes[0] <= 0:
            return REDACTED
        remaining_nodes[0] -= 1

        if isinstance(item, str):
            return redact_operator_text(item, extra_secret_values=secrets)
        if type(item) in (bytes, bytearray, memoryview):
            if type(item) is bytes:
                raw_binary = item
            elif type(item) is bytearray:
                raw_binary = bytes(item)
            else:
                raw_binary = item.tobytes()
            try:
                decoded = raw_binary.decode("utf-8", "strict")
            except UnicodeDecodeError:
                redacted_binary = REDACTED.encode("utf-8")
            else:
                redacted_binary = redact_operator_text(
                    decoded,
                    extra_secret_values=secrets,
                ).encode("utf-8")
            if type(item) is bytes:
                return redacted_binary
            if type(item) is bytearray:
                return bytearray(redacted_binary)
            return memoryview(redacted_binary)

        # Structural presentation is trusted only for exact built-in containers.
        # Container subclasses can override items()/__iter__ and must not gain code
        # execution inside this fail-closed redaction boundary.
        is_mapping = type(item) is dict
        is_list = type(item) is list
        is_tuple = type(item) is tuple
        is_set = type(item) is set
        is_frozenset = type(item) is frozenset
        if is_mapping or is_list or is_tuple or is_set or is_frozenset:
            identity = id(item)
            if identity in active_container_ids:
                return REDACTED
            active_container_ids.add(identity)
            try:
                if is_mapping:
                    redacted: dict[Any, Any] = {}
                    prepared: list[tuple[object, Any, bool, bool]] = []
                    reserved_keys: set[object] = set()

                    # Count each mapping entry before retaining it. If the global
                    # presentation budget is exhausted, fail closed for the whole
                    # container rather than materializing an unbounded partial copy.
                    for key, child in item.items():
                        if remaining_nodes[0] <= 0:
                            return REDACTED
                        remaining_nodes[0] -= 1
                        safe_key, key_is_sensitive, key_was_transformed = (
                            _redact_operator_mapping_key(
                                key,
                                secrets=secrets,
                                _global_remaining_nodes=remaining_nodes,
                            )
                        )
                        prepared.append(
                            (
                                safe_key,
                                child,
                                key_is_sensitive,
                                key_was_transformed,
                            )
                        )
                        if not key_was_transformed:
                            reserved_keys.add(safe_key)

                    for (
                        safe_key,
                        child,
                        key_is_sensitive,
                        key_was_transformed,
                    ) in prepared:
                        if key_was_transformed:
                            candidate = safe_key
                            suffix = 2
                            while (
                                candidate in reserved_keys
                                or candidate in redacted
                            ):
                                candidate = _mapping_key_collision_alias(
                                    safe_key,
                                    suffix,
                                )
                                suffix += 1
                            safe_key = candidate

                        if key_is_sensitive:
                            redacted[safe_key] = REDACTED
                        else:
                            if remaining_nodes[0] <= 0:
                                return REDACTED
                            redacted[safe_key] = redact(
                                child,
                                depth=depth + 1,
                            )
                    return redacted

                if is_list:
                    output: list[Any] = []
                    for child in item:
                        if remaining_nodes[0] <= 0:
                            return REDACTED
                        output.append(redact(child, depth=depth + 1))
                    return output

                if is_tuple:
                    output_tuple: list[Any] = []
                    for child in item:
                        if remaining_nodes[0] <= 0:
                            return REDACTED
                        output_tuple.append(redact(child, depth=depth + 1))
                    return tuple(output_tuple)

                if is_set:
                    output_set: set[Any] = set()
                    for child in item:
                        if remaining_nodes[0] <= 0:
                            return REDACTED
                        output_set.add(redact(child, depth=depth + 1))
                    return output_set

                output_frozen: set[Any] = set()
                for child in item:
                    if remaining_nodes[0] <= 0:
                        return REDACTED
                    output_frozen.add(redact(child, depth=depth + 1))
                return frozenset(output_frozen)
            finally:
                active_container_ids.remove(identity)

        # Only exact inert built-in scalars may cross this presentation boundary
        # unchanged. Arbitrary objects can carry credentials in fields or custom
        # __str__/__repr__/serialization behavior; returning them intact would defer
        # the leak to the next renderer. Fail closed without invoking user code.
        if item is None or type(item) in (bool, int, float, complex):
            return item
        return REDACTED

    return redact(value, depth=0)


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


def _safe_exception_type_label(exc: BaseException) -> str:
    """Return the nearest actual built-in BaseException category.

    Exception class names are untrusted presentation input: dynamically-created
    provider/plugin types can encode arbitrary response data or credentials in
    their class name. A syntactically valid custom identifier is therefore not
    presentation authority. Only exact built-in BaseException classes found in
    the exception MRO may supply the rendered type label.
    """

    try:
        exception_type = type(exc)
        mro = type.__getattribute__(exception_type, "__mro__")
    except BaseException:
        return "BaseException"

    for candidate_type in mro:
        try:
            candidate = str.__str__(
                type.__getattribute__(candidate_type, "__name__")
            )
            builtin_candidate = vars(builtins).get(candidate)
            if (
                builtin_candidate is candidate_type
                and isinstance(candidate_type, type)
                and issubclass(candidate_type, BaseException)
            ):
                return candidate
        except BaseException:
            continue
    return "BaseException"

def safe_exception_text(
    exc: BaseException,
    *,
    unavailable_detail: str = "exception details unavailable",
    extra_secret_values: Iterable[str] = (),
) -> str:
    """Render a bounded Type: detail string and redact secret material."""

    secrets = tuple(extra_secret_values)
    exception_type = _safe_exception_type_label(exc)
    detail = safe_exception_detail(
        exc,
        unavailable_detail=unavailable_detail,
        extra_secret_values=secrets,
    )
    rendered = exception_type if not detail else exception_type + ": " + detail
    return redact_operator_text(
        rendered,
        extra_secret_values=secrets,
    )
