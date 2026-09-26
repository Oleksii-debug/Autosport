from __future__ import annotations

import os
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
