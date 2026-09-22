from __future__ import annotations

import unittest

from autosport.integration_protected_tree import (
    GitTreeEntry,
    ProtectedTreeGateError,
    TrustedProtectedTreePolicy,
    verify_base_trusted_protected_tree,
)

A = "a" * 40
B = "b" * 40
C = "c" * 40
D = "d" * 40


def entry(
    path: str,
    oid: str = A,
    mode: str = "100644",
    kind: str = "blob",
) -> GitTreeEntry:
    return GitTreeEntry(path=path, mode=mode, object_type=kind, object_id=oid)


class ProtectedTreeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = (
            entry(".github/protected-tree-policy.json", A),
            entry("src/autosport/authority.py", B),
            entry("tests/test_authority.py", C),
            entry("README.md", D),
        )
        self.policy = TrustedProtectedTreePolicy(
            protected_paths=(".github/protected-tree-policy.json",),
            protected_prefixes=("src/autosport/", "tests/"),
            content_change_allowlist=("src/autosport/authority.py",),
            immutable_policy_paths=(".github/protected-tree-policy.json",),
        )
        self.manifest = self.base[:3]

    def verify(self, candidate=None, manifest=None, policy=None):
        return verify_base_trusted_protected_tree(
            base_tree=self.base,
            candidate_tree=self.base if candidate is None else candidate,
            trusted_base_manifest=self.manifest if manifest is None else manifest,
            policy=self.policy if policy is None else policy,
        )

    def test_unchanged_protected_tree_passes(self):
        result = self.verify()
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.protected_entry_count, 3)
        self.assertTrue(result.candidate_ci_eligible)
        self.assertFalse(result.merge_authorized)
        self.assertFalse(result.release_authorized)

    def test_allowlisted_content_only_change_passes(self):
        candidate = list(self.base)
        candidate[1] = entry("src/autosport/authority.py", D)
        result = self.verify(candidate=tuple(candidate))
        self.assertEqual(
            result.allowed_content_changes,
            ("src/autosport/authority.py",),
        )

    def test_unprotected_change_is_outside_gate_scope(self):
        candidate = list(self.base)
        candidate[3] = entry("README.md", A)
        self.assertEqual(self.verify(candidate=tuple(candidate)).status, "PASS")

    def test_protected_authority_deletion_rejected(self):
        with self.assertRaisesRegex(ProtectedTreeGateError, "deletes protected"):
            self.verify(candidate=(self.base[0], self.base[2], self.base[3]))

    def test_protected_test_deletion_rejected(self):
        with self.assertRaisesRegex(ProtectedTreeGateError, "deletes protected"):
            self.verify(candidate=(self.base[0], self.base[1], self.base[3]))

    def test_unallowlisted_protected_content_change_rejected(self):
        candidate = list(self.base)
        candidate[2] = entry("tests/test_authority.py", D)
        with self.assertRaisesRegex(
            ProtectedTreeGateError,
            "without base-trusted allowlist",
        ):
            self.verify(candidate=tuple(candidate))

    def test_allowlisted_mode_change_rejected(self):
        candidate = list(self.base)
        candidate[1] = entry(
            "src/autosport/authority.py",
            B,
            "100755",
            "blob",
        )
        with self.assertRaisesRegex(ProtectedTreeGateError, "entry kind"):
            self.verify(candidate=tuple(candidate))

    def test_allowlisted_symlink_change_rejected(self):
        candidate = list(self.base)
        candidate[1] = entry(
            "src/autosport/authority.py",
            B,
            "120000",
            "blob",
        )
        with self.assertRaisesRegex(ProtectedTreeGateError, "entry kind"):
            self.verify(candidate=tuple(candidate))

    def test_submodule_shape_rejected_when_base_is_blob(self):
        candidate = list(self.base)
        candidate[1] = entry(
            "src/autosport/authority.py",
            B,
            "160000",
            "commit",
        )
        with self.assertRaisesRegex(ProtectedTreeGateError, "entry kind"):
            self.verify(candidate=tuple(candidate))

    def test_candidate_new_protected_path_rejected(self):
        candidate = self.base + (entry("src/autosport/new_guard.py", A),)
        with self.assertRaisesRegex(ProtectedTreeGateError, "adds untrusted path"):
            self.verify(candidate=candidate)

    def test_incomplete_trusted_manifest_rejected(self):
        with self.assertRaisesRegex(ProtectedTreeGateError, "manifest is incomplete"):
            self.verify(manifest=self.manifest[:-1])

    def test_extra_trusted_manifest_entry_rejected(self):
        with self.assertRaisesRegex(ProtectedTreeGateError, "manifest is incomplete"):
            self.verify(manifest=self.manifest + (self.base[3],))

    def test_manifest_content_not_matching_base_rejected(self):
        bad = list(self.manifest)
        bad[1] = entry("src/autosport/authority.py", D)
        with self.assertRaisesRegex(
            ProtectedTreeGateError,
            "does not match trusted base",
        ):
            self.verify(manifest=tuple(bad))

    def test_immutable_policy_cannot_be_allowlisted(self):
        with self.assertRaisesRegex(ProtectedTreeGateError, "immutable policy"):
            TrustedProtectedTreePolicy(
                protected_paths=(".github/protected-tree-policy.json",),
                content_change_allowlist=(".github/protected-tree-policy.json",),
                immutable_policy_paths=(".github/protected-tree-policy.json",),
            )

    def test_allowlist_path_must_be_protected(self):
        with self.assertRaisesRegex(
            ProtectedTreeGateError,
            "must already be protected",
        ):
            TrustedProtectedTreePolicy(
                protected_paths=("src/autosport/x.py",),
                content_change_allowlist=("README.md",),
            )

    def test_declared_exact_protected_path_must_exist_in_base(self):
        policy = TrustedProtectedTreePolicy(
            protected_paths=("missing.py",),
        )
        with self.assertRaisesRegex(
            ProtectedTreeGateError,
            "absent from trusted base",
        ):
            self.verify(policy=policy, manifest=())

    def test_malformed_oid_rejected(self):
        with self.assertRaisesRegex(ProtectedTreeGateError, "SHA-1"):
            entry("src/autosport/a.py", "A" * 40)

    def test_mode_type_mismatch_rejected(self):
        with self.assertRaisesRegex(
            ProtectedTreeGateError,
            "unsupported Git entry",
        ):
            entry("src/autosport/a.py", A, "160000", "blob")

    def test_backslash_path_rejected(self):
        with self.assertRaisesRegex(ProtectedTreeGateError, "forbidden"):
            entry(r"src\autosport\a.py", A)

    def test_path_traversal_rejected(self):
        with self.assertRaisesRegex(ProtectedTreeGateError, "traversal"):
            entry("src/../authority.py", A)

    def test_absolute_path_rejected(self):
        with self.assertRaisesRegex(ProtectedTreeGateError, "relative"):
            entry("/src/autosport/a.py", A)

    def test_win32_trailing_dot_alias_rejected(self):
        with self.assertRaisesRegex(ProtectedTreeGateError, "Win32"):
            entry("src/autosport/a.py.", A)

    def test_non_nfc_path_rejected(self):
        decomposed = "tests/cafe\u0301.py"
        with self.assertRaisesRegex(ProtectedTreeGateError, "NFC"):
            entry(decomposed, A)

    def test_case_alias_collision_rejected(self):
        candidate = self.base + (entry("SRC/AUTOSPORT/AUTHORITY.PY", D),)
        with self.assertRaisesRegex(ProtectedTreeGateError, "alias collision"):
            self.verify(candidate=candidate)

    def test_evidence_digest_is_input_order_independent(self):
        first = self.verify()
        second = verify_base_trusted_protected_tree(
            base_tree=tuple(reversed(self.base)),
            candidate_tree=tuple(reversed(self.base)),
            trusted_base_manifest=tuple(reversed(self.manifest)),
            policy=self.policy,
        )
        self.assertEqual(first.evidence_sha256, second.evidence_sha256)


if __name__ == "__main__":
    unittest.main()
