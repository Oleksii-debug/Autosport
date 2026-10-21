from __future__ import annotations

import unittest

from autosport.release_package import _validate_windows_member


class ReleasePackageForbiddenStateMemberTests(unittest.TestCase):
    """ZIP verification must not rely on a self-consistent hash manifest."""

    def test_rejects_private_and_test_state_at_any_depth(self) -> None:
        forbidden = (
            "examples/tt_demo/__pycache__/cache.pyc",
            "examples/tt_demo/.pytest_cache/v/cache/nodeids",
            "examples/tt_demo/.mypy_cache/status.json",
            "examples/tt_demo/.ruff_cache/file",
            "examples/tt_demo/.tox/py311/state",
            "examples/tt_demo/reports/.coverage",
            "examples/tt_demo/reports/.coverage.machine",
            "examples/tt_demo/coverage.xml",
            "examples/tt_demo/cache.pyo",
            "examples/tt_demo/.env",
            "examples/tt_demo/oauth/token.json",
            "examples/tt_demo/browser/User Data/Default/Cookies",
            "examples/tt_demo/.aws/credentials",
            "examples/tt_demo/.ssh/id_ed25519",
            "examples/tt_demo/user.session",
            "examples/tt_demo/user.session-journal",
            "examples/tt_demo/tls/operator.key",
            "examples/tt_demo/tls/operator.pfx",
            "examples/tt_demo/.azure/auth/cache",
            "examples/tt_demo/.mozilla/firefox/profile",
            "examples/tt_demo/profile/cookies.json",
            "examples/tt_demo/profile/cookies.sqlite",
            "examples/tt_demo/profile/credentials.json",
            "examples/tt_demo/profile/application_default_credentials.json",
            "examples/tt_demo/profile/oauth_credentials.json",
            "examples/tt_demo/profile/key3.db",
            "examples/tt_demo/profile/key4.db",
            "examples/tt_demo/profile/local state",
            "examples/tt_demo/profile/web data",
            "examples/tt_demo/profile/client_secret_oauth.json",
            "examples/tt_demo/profile/.env.production",
            "examples/tt_demo/profile/id_dsa",
            "examples/tt_demo/profile/id_ecdsa",
            "examples/tt_demo/profile/id_rsa.pem",
            "examples/tt_demo/profile/private-signing.pem",
        )
        for name in forbidden:
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    ValueError, "forbidden private/test state"
                ):
                    _validate_windows_member("Autosport-V1/" + name)

    def test_case_folding_cannot_bypass_state_filter(self) -> None:
        for name in ("EXAMPLES/__PYCACHE__/C.PYC", "examples/.PYTEST_CACHE/data", "examples/TOKEN.JSON"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "forbidden private/test state"):
                    _validate_windows_member("Autosport-V1/" + name)

    def test_legitimate_product_evidence_remains_permitted(self) -> None:
        permitted = (
            "Autosport.exe",
            "BUILD_INFO.json",
            "PACKAGE_MANIFEST.json",
            "SHA256SUMS.txt",
            "examples/tt_demo/manifest.json",
            "examples/tt_demo/results.json",
            "examples/tt_demo/.env.example",
            "examples/tt_demo/.env.sample",
            "examples/tt_demo/.env.template",
            "examples/tt_demo/.env.dist",
            "examples/tt_demo/certificates/public-signing.pem",
            "examples/tt_demo/public-certificate.pem",
            "examples/tt_demo/market.jsonl",
        )
        for name in permitted:
            with self.subTest(name=name):
                relative, normalized = _validate_windows_member("Autosport-V1/" + name)
                self.assertEqual(relative, name)
                self.assertEqual(normalized, name.casefold())


if __name__ == "__main__":
    unittest.main()
