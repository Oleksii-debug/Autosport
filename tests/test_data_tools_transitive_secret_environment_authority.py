from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from autosport import data_tools_entry, secret_redaction


def _render_with_configured_secret(secret: str) -> str:
    exc = ValueError(f"provider rejected credential value {secret}")
    return data_tools_entry._expected_failure_message(
        "verify-dataset",
        exc,
    )


def _assert_fail_closed(output: str, secret: str) -> None:
    assert "error=ExpectedFailure" in output
    assert "exception details unavailable" in output
    assert secret not in output


def test_transitive_environment_secret_resolver_rebinding_cannot_publish_bare_secret() -> None:
    """Configured-secret authority must not stop at `_secret_values` identity."""

    secret = "AS-DATATOOLS-TRANSITIVE-ENVIRONMENT-SENTINEL-91ad"

    with patch.dict(
        os.environ,
        {"AUTOSPORT_TEST_API_KEY": secret},
        clear=False,
    ), patch.object(
        secret_redaction,
        "_environment_secret_values",
        lambda: (),
    ):
        output = _render_with_configured_secret(secret)

    _assert_fail_closed(output, secret)


def test_transitive_sensitive_key_classifier_rebinding_cannot_publish_bare_secret() -> None:
    """Environment-secret resolution must not trust a rebound key classifier.

    Keeping both `_secret_values` and `_environment_secret_values` function objects and
    code unchanged is insufficient when the latter still late-resolves
    `is_sensitive_key`. A caller that makes every environment key look non-sensitive
    must not disable configured-secret redaction at the packaged operator boundary.
    """

    secret = "AS-DATATOOLS-TRANSITIVE-KEYCLASS-SENTINEL-a71c"

    with patch.dict(
        os.environ,
        {"AUTOSPORT_TEST_API_KEY": secret},
        clear=False,
    ), patch.object(
        secret_redaction,
        "is_sensitive_key",
        lambda _key: False,
    ):
        output = _render_with_configured_secret(secret)

    _assert_fail_closed(output, secret)


def test_transitive_os_module_rebinding_cannot_hide_configured_secret() -> None:
    """The canonical resolver must not late-resolve a substituted `os` module."""

    secret = "AS-DATATOOLS-TRANSITIVE-OS-SENTINEL-b31c"
    with patch.dict(
        os.environ,
        {"AUTOSPORT_TEST_API_KEY": secret},
        clear=False,
    ), patch.object(
        secret_redaction,
        "os",
        SimpleNamespace(environ={}),
    ):
        output = _render_with_configured_secret(secret)

    _assert_fail_closed(output, secret)


def test_transitive_re_sub_rebinding_fails_closed_before_redaction() -> None:
    """Sensitive-key normalization must retain the canonical regex dispatch."""

    secret = "AS-DATATOOLS-TRANSITIVE-RE-SENTINEL-583e"
    with patch.dict(
        os.environ,
        {"AUTOSPORT_TEST_API_KEY": secret},
        clear=False,
    ), patch.object(
        secret_redaction.re,
        "sub",
        lambda _pattern, _replacement, _value: "",
    ):
        output = _render_with_configured_secret(secret)

    _assert_fail_closed(output, secret)


def test_transitive_url_decoder_rebinding_fails_closed_before_redaction() -> None:
    """Query-key classification must retain the canonical URL decoder dispatch."""

    secret = "AS-DATATOOLS-TRANSITIVE-URLDECODER-SENTINEL-e3f4"
    with patch.dict(
        os.environ,
        {"AUTOSPORT_TEST_API_KEY": secret},
        clear=False,
    ), patch.object(
        secret_redaction,
        "unquote_plus",
        lambda _value: "market",
    ):
        output = _render_with_configured_secret(secret)

    _assert_fail_closed(output, secret)


def test_transitive_builtin_len_shadow_cannot_hide_configured_secret() -> None:
    """Helper code identity must not trust a module-global builtin shadow."""

    secret = "AS-DATATOOLS-TRANSITIVE-BUILTIN-LEN-SENTINEL-2c91"
    with patch.dict(
        os.environ,
        {"AUTOSPORT_TEST_API_KEY": secret},
        clear=False,
    ), patch.object(
        secret_redaction,
        "len",
        lambda _value: 0,
        create=True,
    ):
        # Without the presentation guard this makes the canonical environment resolver
        # discard the configured secret while retaining every helper object/code.
        assert secret_redaction._environment_secret_values() == ()
        output = _render_with_configured_secret(secret)

    _assert_fail_closed(output, secret)


def test_transitive_builtin_enumerate_shadow_cannot_skip_configured_secret_redaction() -> None:
    """The redaction loop's builtin enumerate dispatch is presentation authority."""

    secret = "AS-DATATOOLS-TRANSITIVE-BUILTIN-ENUMERATE-SENTINEL-7b42"
    detail = f"provider rejected credential value {secret}"
    with patch.dict(
        os.environ,
        {"AUTOSPORT_TEST_API_KEY": secret},
        clear=False,
    ), patch.object(
        secret_redaction,
        "enumerate",
        lambda _items: (),
        create=True,
    ):
        # Prove the attack is non-vacuous: bypassing the configured-secret replacement
        # loop would expose this bare secret if the Data Tools boundary trusted it.
        assert secret in secret_redaction.redact_operator_text(detail)
        output = _render_with_configured_secret(secret)

    _assert_fail_closed(output, secret)


def test_guard_getattr_shadow_cannot_hide_text_redactor_rebinding() -> None:
    """The guard must not late-resolve its own getattr through module globals."""

    secret = "AS-DATATOOLS-GUARD-GETATTR-SENTINEL-bbe7"
    canonical_text_redactor = secret_redaction.redact_operator_text
    exact_getattr = getattr

    def stale_getattr(value, name: str, default=None):
        if value is secret_redaction and name == "redact_operator_text":
            return canonical_text_redactor
        return exact_getattr(value, name, default)

    with patch.object(
        secret_redaction,
        "redact_operator_text",
        lambda text, **_kwargs: text,
    ), patch.object(
        data_tools_entry,
        "getattr",
        stale_getattr,
        create=True,
    ):
        output = data_tools_entry._expected_failure_message(
            "verify-dataset",
            ValueError(f"Authorization: Bearer {secret}"),
        )

    _assert_fail_closed(output, secret)
