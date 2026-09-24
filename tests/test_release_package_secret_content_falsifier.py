from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autosport.release_package import build_windows_package, verify_windows_package


class ReleasePackageSecretContentFalsifierTests(unittest.TestCase):
    """Require secret-content exclusion at the canonical Windows ZIP boundary."""

    SOURCE_SHA = "a" * 40
    SCAN_CHUNK = 1024 * 1024

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        path.write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def _build_candidate(cls, root: Path, *, example_payload: bytes) -> Path:
        exe = root / "Autosport.exe"
        start = root / "WINDOWS_START_HERE.txt"
        example = root / "tt_demo"
        diagnostic = root / "packaged-diagnostic.json"
        accessibility = root / "accessibility-audit.json"
        keyboard = root / "keyboard-audit.json"
        restart = root / "restart-recovery-audit.json"
        package = root / "Autosport.zip"

        exe.write_bytes(b"synthetic executable fixture")
        start.write_text("Synthetic test start guide.\n", encoding="utf-8")
        example.mkdir()
        (example / "operator-notes.txt").write_bytes(example_payload)

        common_audit = {
            "status": "PASS",
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        for path in (diagnostic, accessibility, keyboard):
            cls._write_json(path, common_audit)

        restart_payload = {
            **common_audit,
            "session_restart_status": "PASS",
            "transaction_recovery_status": "PASS",
            "recovery_disposition": "aborted_uncommitted",
            "process_kill_relaunch_status": "PASS",
            "process_kill_stage_pid": 1001,
            "process_recovery_pid": 1002,
            "process_kill_return_code": -9,
            "process_recovery_run_id": "synthetic-secret-falsifier-run",
            "process_recovery_disposition": "committed",
            "process_recovery_registry_status": "completed",
            "process_recovery_manifest_phase": "completed",
            "process_recovery_base_paper_book_sha256": "1" * 64,
            "process_recovery_base_decision_ledger_sha256": "2" * 64,
            "process_recovery_new_paper_book_sha256": "3" * 64,
            "process_recovery_new_decision_ledger_sha256": "4" * 64,
        }
        cls._write_json(restart, restart_payload)

        build_windows_package(
            exe,
            start,
            example,
            diagnostic,
            accessibility,
            keyboard,
            restart,
            package,
            cls.SOURCE_SHA,
        )
        return package

    def test_control_package_without_secret_content_still_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(
                Path(temporary),
                example_payload=b"synthetic public example data\n",
            )
            result = verify_windows_package(
                package,
                expected_source_sha=self.SOURCE_SHA,
            )
            self.assertEqual(result["status"], "PASS")

    def test_harmless_filename_cannot_hide_secret_assignment_content(self) -> None:
        synthetic_secret = b"not-a-real-secret-" + b"A" * 64
        payload = b'api_key="' + synthetic_secret + b'"\n'
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(
                Path(temporary),
                example_payload=payload,
            )
            with self.assertRaisesRegex(ValueError, "secret|credential"):
                verify_windows_package(
                    package,
                    expected_source_sha=self.SOURCE_SHA,
                )

    def test_long_secret_assignment_crossing_one_mib_boundary_is_rejected(self) -> None:
        marker = b'api_key="'
        synthetic_secret = b"not-a-real-secret-" + b"B" * 600
        payload = (
            b"A" * (self.SCAN_CHUNK - 520)
            + marker
            + synthetic_secret
            + b'"\n'
        )
        marker_offset = payload.index(marker)
        self.assertEqual(marker_offset, self.SCAN_CHUNK - 520)
        self.assertGreater(payload.index(b'"\n', marker_offset), self.SCAN_CHUNK)

        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(
                Path(temporary),
                example_payload=payload,
            )
            with self.assertRaisesRegex(ValueError, "secret|credential"):
                verify_windows_package(
                    package,
                    expected_source_sha=self.SOURCE_SHA,
                )


    def test_environment_references_are_not_embedded_credentials(self) -> None:
        controls = (
            b"api_key=${AUTOSPORT_API_KEY}\n",
            b"client_secret=%CLIENT_SECRET%\n",
            b"access_token=$ACCESS_TOKEN\n",
            b"authorization=$env:AUTOSPORT_AUTH\n",
            b"Authorization: Bearer ${BEARER_TOKEN}\n",
        )
        for payload in controls:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temporary:
                    package = self._build_candidate(
                        Path(temporary),
                        example_payload=payload,
                    )
                    result = verify_windows_package(
                        package,
                        expected_source_sha=self.SOURCE_SHA,
                    )
                    self.assertEqual(result["status"], "PASS")

    def test_json_secret_assignment_is_rejected(self) -> None:
        payload = b'{"api_key": "abcdefghijklmnopqrstuvwxyz012345"}\n'
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(
                Path(temporary),
                example_payload=payload,
            )
            with self.assertRaisesRegex(ValueError, "secret|credential"):
                verify_windows_package(
                    package,
                    expected_source_sha=self.SOURCE_SHA,
                )

    def test_authorization_bearer_and_basic_headers_are_rejected(self) -> None:
        cases = (
            b"Authorization:   Bearer   abcdefghijklmnopqrstuvwxyz012345\n",
            b"Authorization:\tBasic\tabcdefghijklmnopqrstuvwxyz012345\n",
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temporary:
                    package = self._build_candidate(
                        Path(temporary),
                        example_payload=payload,
                    )
                    with self.assertRaisesRegex(ValueError, "secret|credential"):
                        verify_windows_package(
                            package,
                            expected_source_sha=self.SOURCE_SHA,
                        )

    def test_punctuation_leading_concrete_secret_is_rejected(self) -> None:
        payload = b"api_key=!abcdefghijklmnopqrstuvwxyz012345\n"
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(
                Path(temporary),
                example_payload=payload,
            )
            with self.assertRaisesRegex(ValueError, "secret|credential"):
                verify_windows_package(
                    package,
                    expected_source_sha=self.SOURCE_SHA,
                )

    def test_very_long_concrete_secret_has_no_upper_length_escape(self) -> None:
        payload = b"client_secret=" + (b"Z" * 100_000) + b"\n"
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(
                Path(temporary),
                example_payload=payload,
            )
            with self.assertRaisesRegex(ValueError, "secret|credential"):
                verify_windows_package(
                    package,
                    expected_source_sha=self.SOURCE_SHA,
                )

    def test_key_and_value_on_different_lines_are_not_joined(self) -> None:
        payload = b"api_key=\n" + (b"Q" * 100) + b"\n"
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(
                Path(temporary),
                example_payload=payload,
            )
            result = verify_windows_package(
                package,
                expected_source_sha=self.SOURCE_SHA,
            )
            self.assertEqual(result["status"], "PASS")

    def test_extended_high_confidence_key_families_are_rejected(self) -> None:
        keys = (
            b"api_hash",
            b"x-api-key",
            b"x_auth_token",
            b"oauth_secret",
            b"oauth_access_token",
            b"oauth_refresh_token",
            b"oauth2_access_token",
            b"consumer_secret",
            b"session_id",
            b"bot_token",
            b"cookie",
            b"set_cookie",
        )
        for key in keys:
            with self.subTest(key=key):
                payload = key + b"=" + (b"S" * 48) + b"\n"
                with tempfile.TemporaryDirectory() as temporary:
                    package = self._build_candidate(
                        Path(temporary),
                        example_payload=payload,
                    )
                    with self.assertRaisesRegex(ValueError, "secret|credential"):
                        verify_windows_package(
                            package,
                            expected_source_sha=self.SOURCE_SHA,
                        )

    def test_encrypted_private_key_header_is_rejected(self) -> None:
        payload = (
            b"-----BEGIN ENCRYPTED PRIVATE KEY-----\n"
            b"synthetic-fixture-not-a-real-key\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(
                Path(temporary),
                example_payload=payload,
            )
            with self.assertRaisesRegex(ValueError, "secret|credential"):
                verify_windows_package(
                    package,
                    expected_source_sha=self.SOURCE_SHA,
                )


    def test_bom_encoded_secret_assignments_are_rejected(self) -> None:
        synthetic_secret = "not-a-real-secret-" + ("B" * 64)
        text = f"api_key={synthetic_secret}\r\n"
        cases = (
            ("utf-8-sig", text.encode("utf-8-sig")),
            ("utf-16-le", b"\xff\xfe" + text.encode("utf-16-le")),
            ("utf-16-be", b"\xfe\xff" + text.encode("utf-16-be")),
            ("utf-32-le", b"\xff\xfe\x00\x00" + text.encode("utf-32-le")),
            ("utf-32-be", b"\x00\x00\xfe\xff" + text.encode("utf-32-be")),
        )
        for encoding, payload in cases:
            with self.subTest(encoding=encoding):
                with tempfile.TemporaryDirectory() as temporary:
                    package = self._build_candidate(
                        Path(temporary),
                        example_payload=payload,
                    )
                    with self.assertRaisesRegex(ValueError, "secret|credential"):
                        verify_windows_package(
                            package,
                            expected_source_sha=self.SOURCE_SHA,
                        )

    def test_bom_encoded_environment_references_stay_clean(self) -> None:
        text = "api_key=${AUTOSPORT_API_KEY}\r\n"
        cases = (
            ("utf-8-sig", text.encode("utf-8-sig")),
            ("utf-16-le", b"\xff\xfe" + text.encode("utf-16-le")),
            ("utf-16-be", b"\xfe\xff" + text.encode("utf-16-be")),
            ("utf-32-le", b"\xff\xfe\x00\x00" + text.encode("utf-32-le")),
            ("utf-32-be", b"\x00\x00\xfe\xff" + text.encode("utf-32-be")),
        )
        for encoding, payload in cases:
            with self.subTest(encoding=encoding):
                with tempfile.TemporaryDirectory() as temporary:
                    package = self._build_candidate(
                        Path(temporary),
                        example_payload=payload,
                    )
                    result = verify_windows_package(
                        package,
                        expected_source_sha=self.SOURCE_SHA,
                    )
                    self.assertEqual(result["status"], "PASS")

    def test_json_escaped_secret_key_is_rejected(self) -> None:
        synthetic_secret = b"not-a-real-secret-" + b"C" * 64
        payload = b'{"api\\u005fkey":"' + synthetic_secret + b'"}\n'
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(
                Path(temporary),
                example_payload=payload,
            )
            with self.assertRaisesRegex(ValueError, "secret|credential"):
                verify_windows_package(
                    package,
                    expected_source_sha=self.SOURCE_SHA,
                )

    def test_yaml_block_scalar_secret_is_rejected(self) -> None:
        synthetic_secret = b"not-a-real-secret-" + b"D" * 64
        cases = (
            b"api_key: >-\n  " + synthetic_secret + b"\nnext: clean\n",
            b"api_key: |-\n  "
            + synthetic_secret[:30]
            + b"\n  "
            + synthetic_secret[30:]
            + b"\nnext: clean\n",
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temporary:
                    package = self._build_candidate(
                        Path(temporary),
                        example_payload=payload,
                    )
                    with self.assertRaisesRegex(ValueError, "secret|credential"):
                        verify_windows_package(
                            package,
                            expected_source_sha=self.SOURCE_SHA,
                        )

    def test_yaml_block_scalar_environment_reference_stays_clean(self) -> None:
        payload = b"api_key: >-\n  ${AUTOSPORT_API_KEY}\nnext: clean\n"
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(
                Path(temporary),
                example_payload=payload,
            )
            result = verify_windows_package(
                package,
                expected_source_sha=self.SOURCE_SHA,
            )
            self.assertEqual(result["status"], "PASS")

    def test_short_authorization_and_cookie_carriers_are_rejected(self) -> None:
        cases = (
            b"Authorization: Basic dTpw\n",
            b"Authorization: Bearer abc\n",
            b"Cookie: sid=x\n",
            b"Set-Cookie: sid=x; Secure\n",
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temporary:
                    package = self._build_candidate(
                        Path(temporary),
                        example_payload=payload,
                    )
                    with self.assertRaisesRegex(ValueError, "secret|credential"):
                        verify_windows_package(
                            package,
                            expected_source_sha=self.SOURCE_SHA,
                        )

    def test_direct_high_confidence_token_signatures_are_rejected(self) -> None:
        cases = (
            b"github_pat_11AA00ABCDEFGHIJKLMNOPQRSTUVWXYZ\n",
            b"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n",
            b"ASIAABCDEFGHIJKLMNOP\n",
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temporary:
                    package = self._build_candidate(
                        Path(temporary),
                        example_payload=payload,
                    )
                    with self.assertRaisesRegex(ValueError, "secret|credential"):
                        verify_windows_package(
                            package,
                            expected_source_sha=self.SOURCE_SHA,
                        )

    def test_extended_environment_references_stay_clean(self) -> None:
        controls = (
            b"api_key=!AUTOSPORT_RELEASE_SECRET_REFERENCE!\n",
            b"api_key=${env:AUTOSPORT_RELEASE_SECRET_REFERENCE}\n",
            b"api_key=${{ secrets.AUTOSPORT_RELEASE_SECRET_REFERENCE }}\n",
            b"Authorization: Basic !AUTH_TOKEN!\n",
        )
        for payload in controls:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temporary:
                    package = self._build_candidate(
                        Path(temporary),
                        example_payload=payload,
                    )
                    result = verify_windows_package(
                        package,
                        expected_source_sha=self.SOURCE_SHA,
                    )
                    self.assertEqual(result["status"], "PASS")

    def test_compact_inline_separators_and_comments_are_rejected(self) -> None:
        synthetic_secret = b"not-a-real-secret-" + b"E" * 64
        cases = (
            b"echo safe;API_KEY=" + synthetic_secret + b"\n",
            b"echo safe&&set API_KEY=" + synthetic_secret + b"\n",
            b"true||export API_KEY=" + synthetic_secret + b"\n",
            b"const x=1;// API_KEY=" + synthetic_secret + b"\n",
            b"echo safe;# API_KEY=" + synthetic_secret + b"\n",
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as temporary:
                    package = self._build_candidate(
                        Path(temporary),
                        example_payload=payload,
                    )
                    with self.assertRaisesRegex(ValueError, "secret|credential"):
                        verify_windows_package(
                            package,
                            expected_source_sha=self.SOURCE_SHA,
                        )

    def test_token_like_substrings_inside_unrelated_values_stay_clean(self) -> None:
        payload = (
            b"sha256=aaaaaaaaaaaaaaaaaghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789bbbbbbbb\n"
            b"url=https://example.invalid/a#fragment\n"
            b"description=xASIAABCDEFGHIJKLMNOPy\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(
                Path(temporary),
                example_payload=payload,
            )
            result = verify_windows_package(
                package,
                expected_source_sha=self.SOURCE_SHA,
            )
            self.assertEqual(result["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
