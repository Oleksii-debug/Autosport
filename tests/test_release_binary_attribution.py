"""Source-bound conservative PyInstaller module attribution regressions."""
import copy
import unittest

from autosport.release_binary_attribution import (
    capture_binary_module_attribution, validate_binary_module_attribution,
)


class BinaryModuleAttributionTest(unittest.TestCase):
    def setUp(self):
        self.sha = "a" * 40
        self.digest = "b" * 64
        self.graph = {"source_sha": self.sha, "components": [{"name": "example-dist"}]}
        self.inventory = {
            "schema_version": 2, "source_sha": self.sha,
            "executable_sha256": self.digest,
            "evidence_class": "EMBEDDED_PYINSTALLER_CARCHIVE_AND_PYZ_TOC_ONLY",
            "components": [{
                "name": "PYZ-00.pyz", "typecode": "z",
                "packed_bytes": 100, "unpacked_bytes": 500,
            }],
            "pyz_modules": [
                {"name": "autosport.core", "typecode": 0, "position": 1, "packed_bytes": 25},
                {"name": "example.module", "typecode": 0, "position": 27, "packed_bytes": 25},
            ],
        }

    def test_exact_coverage_and_no_clearance(self):
        report = capture_binary_module_attribution(
            self.graph, self.inventory,
            packages_distributions={"example": ["Example_Dist"], "autosport": []},
        )
        self.assertEqual(
            [m["review"] for m in report["modules"]],
            ["UNKNOWN_OWNER_REVIEW_REQUIRED", "HOST_MATCH_REVIEW_REQUIRED"],
        )
        validate_binary_module_attribution(report, self.graph, self.inventory)

    def test_missing_sealed_module_refused(self):
        report = capture_binary_module_attribution(
            self.graph, self.inventory, packages_distributions={},
        )
        report["modules"].pop()
        with self.assertRaises(ValueError):
            validate_binary_module_attribution(report, self.graph, self.inventory)

    def test_fake_clearance_and_untrusted_distribution_refused(self):
        report = capture_binary_module_attribution(
            self.graph, self.inventory, packages_distributions={},
        )
        forged = copy.deepcopy(report)
        forged["modules"][0]["review"] = "LICENSE_CLEARED"
        with self.assertRaises(ValueError):
            validate_binary_module_attribution(forged, self.graph, self.inventory)
        forged = copy.deepcopy(report)
        forged["modules"][0]["host_candidates"] = ["not-in-graph"]
        with self.assertRaises(ValueError):
            validate_binary_module_attribution(forged, self.graph, self.inventory)

    def test_unbounded_and_malformed_build_host_claims_refused(self):
        with self.assertRaises(ValueError):
            capture_binary_module_attribution(
                self.graph, self.inventory,
                packages_distributions={"example": ["example-dist"] * 33},
            )
        with self.assertRaises(ValueError):
            capture_binary_module_attribution(
                self.graph, self.inventory,
                packages_distributions={"example": ["invalid/name"]},
            )


    def test_unhashable_and_mixed_owner_candidates_are_validation_errors(self):
        report = capture_binary_module_attribution(
            self.graph, self.inventory, packages_distributions={},
        )
        for hostile in ([["unhashable"]], [{"nested": "dict"}], ["example-dist", None]):
            with self.subTest(hostile=hostile):
                forged = copy.deepcopy(report)
                forged["modules"][0]["host_candidates"] = hostile
                with self.assertRaises(ValueError):
                    validate_binary_module_attribution(forged, self.graph, self.inventory)


if __name__ == "__main__":
    unittest.main()
