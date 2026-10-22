from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import package_windows


class PackageWindowsFinalDigestBindingTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40

    def setUp(self) -> None:
        # Digest-binding tests use a synthetic SHA and stub the prior source
        # admission stage. Source manifest parsing/validation has own tests.
        source_manifest = {"schema_version": 1, "source_sha": self.SOURCE_SHA}
        source_patch = patch.object(
            package_windows,
            "_source_declaration_manifest",
            return_value=source_manifest,
        )
        source_patch.start()
        self.addCleanup(source_patch.stop)
        # This suite is only about final-package SHA binding. The independent
        # direct/graph dependency admission is tested in dedicated suites and
        # requires genuine build-host pins, which synthetic SHA fixtures lack.
        # Stub *both* build-host capture boundaries explicitly, not the final
        # digest verifiers whose failure/order semantics we exercise here.
        direct_patch = patch.object(
            package_windows, "capture_direct_installed_dependencies",
            return_value={"evidence_class": "TEST_ONLY_DIRECT_DEPENDENCIES"},
        )
        graph_patch = patch.object(
            package_windows, "capture_build_host_dependency_graph",
            return_value={"evidence_class": "TEST_ONLY_REQUIREMENT_GRAPH"},
        )
        direct_patch.start()
        graph_patch.start()
        self.addCleanup(direct_patch.stop)
        self.addCleanup(graph_patch.stop)
        # This suite isolates the final digest gate, not earlier binary/license
        # admission. Those boundaries have their own adversarial tests. The
        # official packager now seals TWO PE32+ executables and requires both
        # nested PYZ inventories, host attribution and license evidence before
        # checking the final package digest. Synthetic SHA-only fixtures cannot
        # satisfy those independent preflight contracts.
        for name, evidence in (
            ("capture_dependency_license_evidence",
             ({"evidence_class": "TEST_ONLY_LICENSE_TEXT"}, {})),
            ("capture_embedded_executable_inventory",
             {"evidence_class": "TEST_ONLY_EMBEDDED_CENSUS"}),
            ("capture_binary_module_attribution",
             {"evidence_class": "TEST_ONLY_BINARY_ATTRIBUTION"}),
        ):
            admission_patch = patch.object(
                package_windows, name, return_value=evidence,
            )
            admission_patch.start()
            self.addCleanup(admission_patch.stop)
        pe_patch = patch.object(package_windows, "_require_executable_pe32plus")
        pe_patch.start()
        self.addCleanup(pe_patch.stop)

    @staticmethod
    def _argv(root: Path) -> list[str]:
        hashes = {
            "exe": "1" * 64,
            "data": "2" * 64,
            "diagnostic": "3" * 64,
            "accessibility": "4" * 64,
            "keyboard": "5" * 64,
            "restart": "6" * 64,
        }
        return [
            "package_windows.py",
            "--exe",
            str(root / "Autosport.exe"),
            "--exe-sha256",
            hashes["exe"],
            "--data-exe",
            str(root / "Autosport-Data.exe"),
            "--data-exe-sha256",
            hashes["data"],
            "--start-file",
            str(root / "WINDOWS_START_HERE.txt"),
            "--example-dir",
            str(root / "example"),
            "--diagnostic",
            str(root / "diagnostic.json"),
            "--diagnostic-sha256",
            hashes["diagnostic"],
            "--accessibility-audit",
            str(root / "accessibility.json"),
            "--accessibility-audit-sha256",
            hashes["accessibility"],
            "--keyboard-audit",
            str(root / "keyboard.json"),
            "--keyboard-audit-sha256",
            hashes["keyboard"],
            "--restart-recovery-audit",
            str(root / "restart.json"),
            "--restart-recovery-audit-sha256",
            hashes["restart"],
            "--output",
            str(root / "candidate.zip"),
            "--source-sha",
            PackageWindowsFinalDigestBindingTests.SOURCE_SHA,
            "--verification-output",
            str(root / "verification.json"),
        ]

    @staticmethod
    def _capture_passthrough(source: Path, *_args, **_kwargs) -> Path:
        return source

    def _common_patches(self, root: Path, output: Path):
        return (
            patch.object(sys, "argv", self._argv(root)),
            patch.object(package_windows, "_bind_source_sha_to_checkout"),
            patch.object(package_windows, "_require_checkout_matches_exact_source"),
            patch.object(
                package_windows,
                "_materialize_exact_static_payload",
                return_value=(
                    root / "WINDOWS_START_HERE.txt",
                    root / "example",
                ),
            ),
            patch.object(
                package_windows,
                "_capture_verified_executable",
                side_effect=self._capture_passthrough,
            ),
            patch.object(
                package_windows,
                "_capture_verified_evidence",
                side_effect=self._capture_passthrough,
            ),
            patch.object(
                package_windows,
                "build_windows_package",
                return_value=(output, "0" * 64),
            ),
        )

    def test_digest_helper_rejects_stale_binding(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "bound package digest does not match the exact verified package snapshot",
        ):
            package_windows._require_verified_package_digest(
                {"package_sha256": "1" * 64},
                {"package_sha256": "2" * 64},
            )

    def test_main_rejects_release_snapshot_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "candidate.zip"
            argv_patch, source_patch, checkout_patch, static_patch, exe_patch, evidence_patch, build_patch = (
                self._common_patches(root, output)
            )
            with (
                argv_patch,
                source_patch,
                checkout_patch,
                static_patch,
                exe_patch,
                evidence_patch,
                build_patch,
                patch.object(
                    package_windows,
                    "bind_portable_data_tool",
                    return_value={"package_sha256": "1" * 64},
                ),
                patch.object(
                    package_windows,
                    "verify_windows_package",
                    return_value={"package_sha256": "2" * 64},
                ),
                patch.object(package_windows, "verify_portable_data_tool") as data_verify,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "bound package digest does not match the exact verified package snapshot",
                ):
                    package_windows.main()
            data_verify.assert_not_called()
            self.assertFalse((root / "verification.json").exists())

    def test_main_rejects_portable_snapshot_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "candidate.zip"
            digest = "1" * 64
            argv_patch, source_patch, checkout_patch, static_patch, exe_patch, evidence_patch, build_patch = (
                self._common_patches(root, output)
            )
            with (
                argv_patch,
                source_patch,
                checkout_patch,
                static_patch,
                exe_patch,
                evidence_patch,
                build_patch,
                patch.object(
                    package_windows,
                    "bind_portable_data_tool",
                    return_value={"package_sha256": digest},
                ),
                patch.object(
                    package_windows,
                    "verify_windows_package",
                    return_value={"package_sha256": digest},
                ),
                patch.object(
                    package_windows,
                    "verify_portable_data_tool",
                    return_value={"package_sha256": "2" * 64},
                ),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "bound package digest does not match the exact verified package snapshot",
                ):
                    package_windows.main()
            self.assertFalse((root / "verification.json").exists())

    def test_main_emits_digest_only_after_both_verifiers_match_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "candidate.zip"
            digest = "4" * 64
            release_verification = {
                "status": "PASS",
                "source_sha": self.SOURCE_SHA,
                "package_sha256": digest,
                "autosport_exe_sha256": "5" * 64,
                "real_money_execution": False,
                "human_tested": False,
                "nvda_verified": False,
            }
            data_verification = {
                "status": "PASS",
                "package_sha256": digest,
                "autosport_data_exe_sha256": "6" * 64,
                "portable_historical_data_tools": True,
            }
            expected_report = dict(release_verification)
            expected_report.update(data_verification)
            expected_report["package_sha256"] = digest
            argv_patch, source_patch, checkout_patch, static_patch, exe_patch, evidence_patch, build_patch = (
                self._common_patches(root, output)
            )
            with (
                argv_patch,
                source_patch,
                checkout_patch,
                static_patch,
                exe_patch,
                evidence_patch,
                build_patch,
                patch.object(
                    package_windows,
                    "bind_portable_data_tool",
                    return_value={"package_sha256": digest},
                ) as bind_mock,
                patch.object(
                    package_windows,
                    "verify_windows_package",
                    return_value=release_verification,
                ),
                patch.object(
                    package_windows,
                    "verify_portable_data_tool",
                    return_value=data_verification,
                ),
                patch("builtins.print") as print_mock,
            ):
                self.assertEqual(package_windows.main(), 0)

            bind_mock.assert_called_once_with(
                output,
                root / "Autosport-Data.exe",
                expected_base_package_sha256="0" * 64,
                data_executable_inventory={
                    "evidence_class": "TEST_ONLY_EMBEDDED_CENSUS",
                },
                data_binary_module_attribution={
                    "evidence_class": "TEST_ONLY_BINARY_ATTRIBUTION",
                },
            )
            report = json.loads((root / "verification.json").read_text(encoding="utf-8"))
            self.assertEqual(report, expected_report)
            print_mock.assert_any_call(f"SHA256={digest}")


if __name__ == "__main__":
    unittest.main()
