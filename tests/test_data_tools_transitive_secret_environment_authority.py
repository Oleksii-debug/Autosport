from __future__ import annotations

import os
from unittest.mock import patch

from autosport import data_tools_entry, secret_redaction


def test_transitive_environment_secret_resolver_rebinding_cannot_publish_bare_secret() -> None:
    """Configured-secret authority must not stop at `_secret_values` identity.

    `_secret_values` late-resolves `_environment_secret_values` through module globals.
    Rebinding that dependency leaves the already-witnessed `_secret_values` function
    object and its code unchanged, so the Data Tools boundary must still fail closed
    rather than publishing a configured bare secret.
    """

    secret = "AS-DATATOOLS-TRANSITIVE-ENVIRONMENT-SENTINEL-91ad"
    exc = ValueError(f"provider rejected credential value {secret}")

    with patch.dict(
        os.environ,
        {"AUTOSPORT_TEST_API_KEY": secret},
        clear=False,
    ), patch.object(
        secret_redaction,
        "_environment_secret_values",
        lambda: (),
    ):
        output = data_tools_entry._expected_failure_message(
            "verify-dataset",
            exc,
        )

    assert "error=ExpectedFailure" in output
    assert "exception details unavailable" in output
    assert secret not in output
