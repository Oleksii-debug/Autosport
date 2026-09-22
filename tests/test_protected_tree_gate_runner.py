from __future__ import annotations

import json
import unittest

from scripts import verify_protected_tree_gate as runner


A = "a" * 40
B = "b" * 40


def policy_payload() -> dict:
    return {
        "schema_version": 1,
        "protected_paths": [
            ".gitattributes",
            ".github/protected-tree-policy.json",
            "pyproject.toml",
            "scripts/verify_protected_tree_gate.py",
            "scripts/verify_source_checkout.py",
            "src/autosport/integration_protected_tree.py",
            "tests/test_integration_protected_tree.py",
            "tests/test_protected_tree_gate_runner.py",
        ],
        "protected_prefixes": [".github/workflows/"],
        "content_change_allowlist": [],
        "immutable_policy_paths": [
            ".github/protected-tree-policy.json",
            "scripts/verify_protected_tree_gate.py",
        ],
    }


class ProtectedTreeGateRunnerTests(unittest.TestCase):
    def test_canonical_policy_wiring_is_accepted(self) -> None:
        policy = runner._decode_policy_bytes(
            (json.dumps(policy_payload()) + "\n").encode("utf-8")
        )
        self.assertTrue(policy.protects(".github/workflows/ci.yml"))
        self.assertTrue(policy.protects("scripts/verify_protected_tree_gate.py"))

    def test_duplicate_policy_key_fails_closed(self) -> None:
        raw = b'{"schema_version":1,"schema_version":1}'
        with self.assertRaisesRegex(runner.ProtectedTreeRunnerError, "duplicate"):
            runner._decode_policy_bytes(raw)

    def test_policy_cannot_drop_workflow_prefix(self) -> None:
        payload = policy_payload()
        payload["protected_prefixes"] = []
        with self.assertRaisesRegex(
            runner.ProtectedTreeRunnerError,
            "workflow paths",
        ):
            runner._decode_policy_bytes(json.dumps(payload).encode("utf-8"))

    def test_runner_cannot_be_content_change_allowlisted(self) -> None:
        payload = policy_payload()
        payload["content_change_allowlist"] = [
            "scripts/verify_protected_tree_gate.py"
        ]
        with self.assertRaisesRegex(
            runner.ProtectedTreeRunnerError,
            "cannot be content-change allowlisted",
        ):
            runner._decode_policy_bytes(json.dumps(payload).encode("utf-8"))

    def test_ls_tree_parser_preserves_blob_and_submodule_shapes(self) -> None:
        payload = (
            b"100644 blob " + A.encode("ascii") + b"\talpha.py\0"
            + b"160000 commit " + B.encode("ascii") + b"\tvendor/x\0"
        )
        entries = runner._parse_ls_tree(payload, label="candidate")
        self.assertEqual(
            [(item.path, item.mode, item.object_type, item.object_id) for item in entries],
            [
                ("alpha.py", "100644", "blob", A),
                ("vendor/x", "160000", "commit", B),
            ],
        )

    def test_ls_tree_non_utf8_path_fails_closed(self) -> None:
        payload = b"100644 blob " + A.encode("ascii") + b"\tbad-\xff.py\0"
        with self.assertRaisesRegex(
            runner.ProtectedTreeRunnerError,
            "non-UTF-8",
        ):
            runner._parse_ls_tree(payload, label="candidate")

    def test_wrapper_evidence_binds_exact_candidate_sha(self) -> None:
        result = runner.ProtectedTreeGateResult(
            status="PASS",
            protected_entry_count=9,
            allowed_content_changes=(),
            evidence_sha256="c" * 64,
            candidate_ci_eligible=True,
        )
        first = runner._build_wrapper_evidence(
            repository="Oleksii-debug/Autosport",
            pr_number=42,
            base_sha=A,
            candidate_sha=B,
            result=result,
        )
        second = runner._build_wrapper_evidence(
            repository="Oleksii-debug/Autosport",
            pr_number=42,
            base_sha=A,
            candidate_sha="d" * 40,
            result=result,
        )
        self.assertNotEqual(
            first["wrapper_evidence_sha256"],
            second["wrapper_evidence_sha256"],
        )
        self.assertEqual(first["base_sha"], A)
        self.assertEqual(first["candidate_sha"], B)
        self.assertFalse(first["merge_authorized"])
        self.assertFalse(first["release_authorized"])

    def test_non_ci_only_result_cannot_be_wrapped(self) -> None:
        result = runner.ProtectedTreeGateResult(
            status="PASS",
            protected_entry_count=9,
            allowed_content_changes=(),
            evidence_sha256="c" * 64,
            candidate_ci_eligible=True,
            merge_authorized=True,
        )
        with self.assertRaisesRegex(
            runner.ProtectedTreeRunnerError,
            "candidate-CI-only",
        ):
            runner._build_wrapper_evidence(
                repository="Oleksii-debug/Autosport",
                pr_number=42,
                base_sha=A,
                candidate_sha=B,
                result=result,
            )


if __name__ == "__main__":
    unittest.main()
