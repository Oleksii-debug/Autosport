from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autosport.operator_config_migration import (
    ConfigBackupIntegrityError,
    ConfigMigrationChainError,
    ConfigMigrationStep,
    ConfigPublicationUncertainError,
    ConfigSchemaError,
    OperatorConfigMigrator,
)


def raw(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ).encode("utf-8") + b"\n"


def migrator_v3(*, validator=None) -> OperatorConfigMigrator:
    def one_to_two(value: dict) -> dict:
        value["display_language"] = "uk-UA"
        return value

    def two_to_three(value: dict) -> dict:
        value["poll_seconds"] = 30
        return value

    return OperatorConfigMigrator(
        target_version=3,
        steps=[
            ConfigMigrationStep(
                1, 2, one_to_two, frozenset({"display_language"})
            ),
            ConfigMigrationStep(
                2, 3, two_to_three, frozenset({"poll_seconds"})
            ),
        ],
        validator=validator,
    )


class OperatorConfigMigrationTests(unittest.TestCase):
    def write_config(self, root: Path, payload: dict, name: str = "оператор config.json") -> Path:
        path = root / "папка з пробілами" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw(payload))
        return path

    def test_preview_has_zero_filesystem_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 1, "custom": {"x": 1}})
            before = path.read_bytes()
            siblings = sorted(p.name for p in path.parent.iterdir())
            plan = migrator_v3().preview(path)
            self.assertTrue(plan.changed)
            self.assertEqual(plan.from_version, 1)
            self.assertEqual(plan.to_version, 3)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), siblings)

    def test_migration_preserves_unknown_fields_and_unicode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(
                Path(tmp),
                {
                    "schema_version": 1,
                    "custom": {"невідоме": ["значення", 7]},
                    "secret_extension": "не логувати",
                },
            )
            result = migrator_v3().migrate(path)
            reopened = json.loads(path.read_text("utf-8"))
            self.assertEqual(result.status, "MIGRATED")
            self.assertEqual(reopened["schema_version"], 3)
            self.assertEqual(reopened["display_language"], "uk-UA")
            self.assertEqual(reopened["poll_seconds"], 30)
            self.assertEqual(reopened["custom"], {"невідоме": ["значення", 7]})
            self.assertEqual(reopened["secret_extension"], "не логувати")

    def test_missing_explicit_step_fails_closed_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 1, "x": 1})
            before = path.read_bytes()
            m = OperatorConfigMigrator(
                target_version=3,
                steps=[ConfigMigrationStep(1, 2, lambda v: v)],
            )
            with self.assertRaises(ConfigMigrationChainError):
                m.migrate(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_downgrade_is_never_implicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 4})
            before = path.read_bytes()
            with self.assertRaisesRegex(ConfigMigrationChainError, "downgrade"):
                migrator_v3().migrate(path)
            self.assertEqual(path.read_bytes(), before)

    def test_repeat_migration_is_idempotent_and_creates_no_second_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 1, "x": 1})
            first = migrator_v3().migrate(path)
            siblings_after_first = sorted(p.name for p in path.parent.iterdir())
            second = migrator_v3().migrate(path)
            self.assertEqual(first.status, "MIGRATED")
            self.assertEqual(second.status, "NO_CHANGE")
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), siblings_after_first)

    def test_invalid_utf8_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_bytes(b"\xff\xfe")
            with self.assertRaises(ConfigSchemaError):
                migrator_v3().preview(path)

    def test_duplicate_json_keys_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_bytes(b'{"schema_version":1,"x":1,"x":2}\n')
            with self.assertRaisesRegex(ConfigSchemaError, "duplicate"):
                migrator_v3().preview(path)

    def test_nonfinite_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_bytes(b'{"schema_version":1,"x":NaN}\n')
            with self.assertRaisesRegex(ConfigSchemaError, "non-finite"):
                migrator_v3().preview(path)

    def test_non_object_root_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_bytes(b'[1,2,3]\n')
            with self.assertRaisesRegex(ConfigSchemaError, "root"):
                migrator_v3().preview(path)

    def test_boolean_schema_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_bytes(b'{"schema_version":true}\n')
            with self.assertRaisesRegex(ConfigSchemaError, "positive integer"):
                migrator_v3().preview(path)

    def test_validator_rejects_candidate_before_backup_or_replace(self) -> None:
        def validator(value):
            if value.get("poll_seconds") == 30:
                raise ValueError("candidate blocked")

        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 1, "x": 1})
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                migrator_v3(validator=validator).migrate(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_step_cannot_change_undeclared_unknown_field(self) -> None:
        def bad(value: dict) -> dict:
            value["unknown"] = "changed"
            return value
        m = OperatorConfigMigrator(
            target_version=2,
            steps=[ConfigMigrationStep(1, 2, bad, frozenset())],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 1, "unknown": "original"})
            with self.assertRaisesRegex(ConfigSchemaError, "undeclared"):
                m.migrate(path)
            self.assertEqual(json.loads(path.read_text("utf-8"))["unknown"], "original")

    def test_step_cannot_drop_undeclared_unknown_field(self) -> None:
        def bad(value: dict) -> dict:
            del value["unknown"]
            return value
        m = OperatorConfigMigrator(
            target_version=2,
            steps=[ConfigMigrationStep(1, 2, bad, frozenset())],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 1, "unknown": {"a": 1}})
            with self.assertRaises(ConfigSchemaError):
                m.migrate(path)

    def test_backup_contains_exact_original_bytes_and_digest_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 1, "x": "є"})
            original = path.read_bytes()
            result = migrator_v3().migrate(path)
            digest = hashlib.sha256(original).hexdigest()
            backup = path.with_name(f".{path.name}.backup-v1-{digest}.json")
            self.assertEqual(result.backup_sha256, digest)
            self.assertEqual(backup.read_bytes(), original)

    def test_replace_failure_preserves_original_and_valid_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 1, "x": 1})
            original = path.read_bytes()
            digest = hashlib.sha256(original).hexdigest()
            real_replace = os.replace
            def fail_replace(src, dst):
                if Path(dst) == path:
                    raise OSError("injected replace failure")
                return real_replace(src, dst)
            with patch("autosport.operator_config_migration.os.replace", side_effect=fail_replace):
                with self.assertRaises(OSError):
                    migrator_v3().migrate(path)
            self.assertEqual(path.read_bytes(), original)
            backup = path.with_name(f".{path.name}.backup-v1-{digest}.json")
            self.assertEqual(backup.read_bytes(), original)
            self.assertFalse(any(".migration-" in p.name for p in path.parent.iterdir()))

    def test_post_replace_readback_failure_is_explicit_uncertainty_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 1, "x": 1})
            original = path.read_bytes()
            digest = hashlib.sha256(original).hexdigest()
            from autosport import operator_config_migration as mod
            real_reader = mod._read_regular_bytes
            seen = {"replaced": False}
            real_replace = mod.os.replace
            def tracking_replace(src, dst):
                result = real_replace(src, dst)
                if Path(dst) == path:
                    seen["replaced"] = True
                return result
            def reader(p, *, kind):
                if Path(p) == path and seen["replaced"]:
                    raise OSError("injected readback failure")
                return real_reader(p, kind=kind)
            with patch.object(mod.os, "replace", side_effect=tracking_replace), patch.object(
                mod, "_read_regular_bytes", side_effect=reader
            ):
                with self.assertRaises(ConfigPublicationUncertainError):
                    migrator_v3().migrate(path)
            reopened = json.loads(path.read_text("utf-8"))
            self.assertEqual(reopened["schema_version"], 3)
            backup = path.with_name(f".{path.name}.backup-v1-{digest}.json")
            self.assertEqual(backup.read_bytes(), original)

    def test_post_replace_directory_fsync_failure_is_publication_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 1, "x": 1})
            original = path.read_bytes()
            digest = hashlib.sha256(original).hexdigest()
            from autosport import operator_config_migration as mod
            calls = {"count": 0}
            real_fsync_parent = mod._fsync_parent
            def fail_after_replace(p):
                calls["count"] += 1
                # First call is backup-name durability, second follows destination replace.
                if calls["count"] == 2:
                    raise OSError("injected directory fsync failure")
                return real_fsync_parent(p)
            with patch.object(mod, "_fsync_parent", side_effect=fail_after_replace):
                with self.assertRaises(ConfigPublicationUncertainError):
                    migrator_v3().migrate(path)
            self.assertEqual(json.loads(path.read_text("utf-8"))["schema_version"], 3)
            backup = path.with_name(f".{path.name}.backup-v1-{digest}.json")
            self.assertEqual(backup.read_bytes(), original)

    def test_restore_verifies_backup_digest_before_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 1, "x": 1})
            original = path.read_bytes()
            journal = migrator_v3().migrate(path)
            current = path.read_bytes()
            backup = path.with_name(f".{path.name}.backup-v1-{journal.backup_sha256}.json")
            backup.write_bytes(b'{"schema_version":1,"x":999}\n')
            with self.assertRaises(ConfigBackupIntegrityError):
                migrator_v3().restore(
                    path,
                    backup_sha256=journal.backup_sha256,
                    backup_from_version=1,
                )
            self.assertEqual(path.read_bytes(), current)
            self.assertNotEqual(path.read_bytes(), original)

    def test_restore_atomically_reinstates_exact_original_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 1, "x": "старе"})
            original = path.read_bytes()
            journal = migrator_v3().migrate(path)
            restored = migrator_v3().restore(
                path,
                backup_sha256=journal.backup_sha256,
                backup_from_version=1,
            )
            self.assertEqual(restored.status, "RESTORED")
            self.assertEqual(restored.to_version, 1)
            self.assertEqual(path.read_bytes(), original)

    def test_existing_wrong_backup_at_digest_bound_name_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 1, "x": 1})
            original = path.read_bytes()
            digest = hashlib.sha256(original).hexdigest()
            backup = path.with_name(f".{path.name}.backup-v1-{digest}.json")
            backup.write_bytes(b"substituted")
            with self.assertRaises(ConfigBackupIntegrityError):
                migrator_v3().migrate(path)
            self.assertEqual(path.read_bytes(), original)

    def test_public_journal_contains_no_path_or_config_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            secret = "TOP-SECRET-VALUE"
            path = self.write_config(Path(tmp), {"schema_version": 1, "secret": secret})
            journal = migrator_v3().migrate(path)
            public = dict(journal.as_public_record())
            rendered = repr(public)
            self.assertNotIn(str(path), rendered)
            self.assertNotIn(secret, rendered)
            self.assertEqual(
                set(public),
                {
                    "status",
                    "from_version",
                    "to_version",
                    "before_sha256",
                    "after_sha256",
                    "backup_sha256",
                },
            )

    def test_symlink_config_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real.json"
            real.write_bytes(raw({"schema_version": 1}))
            link = root / "link.json"
            try:
                link.symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation unavailable")
            with self.assertRaisesRegex(ConfigSchemaError, "non-symlink"):
                migrator_v3().preview(link)

    def test_backup_sha_must_be_canonical_lowercase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_config(Path(tmp), {"schema_version": 3})
            with self.assertRaises(ConfigBackupIntegrityError):
                migrator_v3().restore(
                    path,
                    backup_sha256="A" * 64,
                    backup_from_version=1,
                )


if __name__ == "__main__":
    unittest.main()
