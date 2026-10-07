from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import autosport.source_rights_manifest as source_rights_module
from autosport.source_rights_manifest import (
    SourceRightsAuthorization,
    SourceRightsManifest,
    SourceRightsManifestError,
    authorize_source_use,
    load_source_rights_manifest,
)


class SourceRightsManifestTests(unittest.TestCase):
    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "autosport_source_rights_manifest",
            "source_identity": "licensed-provider-account:fixture",
            "authorized_scopes": [
                "historical.internal_research",
                "historical.read",
            ],
            "effective_at": "2026-09-01T00:00:00Z",
            "expires_at": "2026-12-01T00:00:00Z",
            "human_approved": True,
            "approved_by": "release-owner",
            "approval_reference": "entitlement-record:fixture-001",
            "approved_at": "2026-08-31T12:00:00Z",
        }

    def _write(self, root: Path, payload: dict[str, object] | None = None) -> Path:
        path = root / "source-rights.json"
        path.write_text(
            json.dumps(
                self._payload() if payload is None else payload,
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    def test_exact_source_scope_and_active_interval_are_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp))
            manifest = load_source_rights_manifest(path)

            decision = authorize_source_use(
                manifest,
                source_identity="licensed-provider-account:fixture",
                required_scope="historical.read",
                at=datetime(2026, 9, 21, tzinfo=timezone.utc),
            )

            self.assertEqual(
                decision.source_identity,
                "licensed-provider-account:fixture",
            )
            self.assertEqual(decision.required_scope, "historical.read")
            self.assertEqual(
                decision.manifest_sha256,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            self.assertEqual(decision.approved_by, "release-owner")
            self.assertEqual(
                manifest.authorized_scopes,
                ("historical.internal_research", "historical.read"),
            )

    def test_source_identity_must_match_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = load_source_rights_manifest(self._write(Path(tmp)))
            with self.assertRaisesRegex(
                SourceRightsManifestError,
                "source_identity is not authorized",
            ):
                authorize_source_use(
                    manifest,
                    source_identity="licensed-provider-account:other",
                    required_scope="historical.read",
                    at=datetime(2026, 9, 21, tzinfo=timezone.utc),
                )

    def test_scope_must_be_explicit_exact_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = load_source_rights_manifest(self._write(Path(tmp)))
            with self.assertRaisesRegex(
                SourceRightsManifestError,
                "required_scope is not explicitly authorized",
            ):
                authorize_source_use(
                    manifest,
                    source_identity=manifest.source_identity,
                    required_scope="historical.redistribute",
                    at=datetime(2026, 9, 21, tzinfo=timezone.utc),
                )

    def test_wildcard_manifest_scope_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload()
            payload["authorized_scopes"] = ["historical.*"]
            with self.assertRaisesRegex(
                SourceRightsManifestError,
                "wildcard scopes are forbidden",
            ):
                load_source_rights_manifest(self._write(Path(tmp), payload))

    def test_effective_time_is_inclusive_and_expiry_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = load_source_rights_manifest(self._write(Path(tmp)))
            effective = datetime(2026, 9, 1, tzinfo=timezone.utc)
            expires = datetime(2026, 12, 1, tzinfo=timezone.utc)

            authorize_source_use(
                manifest,
                source_identity=manifest.source_identity,
                required_scope="historical.read",
                at=effective,
            )

            with self.assertRaisesRegex(
                SourceRightsManifestError,
                "not yet effective",
            ):
                authorize_source_use(
                    manifest,
                    source_identity=manifest.source_identity,
                    required_scope="historical.read",
                    at=datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc),
                )

            with self.assertRaisesRegex(
                SourceRightsManifestError,
                "has expired",
            ):
                authorize_source_use(
                    manifest,
                    source_identity=manifest.source_identity,
                    required_scope="historical.read",
                    at=expires,
                )

    def test_human_approval_must_be_explicit_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload()
            payload["human_approved"] = False
            with self.assertRaisesRegex(
                SourceRightsManifestError,
                "human_approved=true",
            ):
                load_source_rights_manifest(self._write(Path(tmp), payload))

    def test_approval_cannot_retroactively_postdate_effective_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload()
            payload["approved_at"] = "2026-09-02T00:00:00Z"
            with self.assertRaisesRegex(
                SourceRightsManifestError,
                "approved_at must not be later than effective_at",
            ):
                load_source_rights_manifest(self._write(Path(tmp), payload))

    def test_expiry_must_be_strictly_after_effective_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload()
            payload["expires_at"] = payload["effective_at"]
            with self.assertRaisesRegex(
                SourceRightsManifestError,
                "expires_at must be later than effective_at",
            ):
                load_source_rights_manifest(self._write(Path(tmp), payload))

    def test_timestamps_require_explicit_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload()
            payload["effective_at"] = "2026-09-01T00:00:00"
            with self.assertRaisesRegex(
                SourceRightsManifestError,
                "effective_at must include an explicit timezone",
            ):
                load_source_rights_manifest(self._write(Path(tmp), payload))

    def test_duplicate_scope_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload()
            payload["authorized_scopes"] = ["historical.read", "historical.read"]
            with self.assertRaisesRegex(
                SourceRightsManifestError,
                "must not contain duplicates",
            ):
                load_source_rights_manifest(self._write(Path(tmp), payload))

    def test_unknown_or_missing_schema_fields_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unknown = self._payload()
            unknown["legal_conclusion"] = "allowed"
            with self.assertRaisesRegex(SourceRightsManifestError, "unknown=legal_conclusion"):
                load_source_rights_manifest(self._write(root, unknown))

            missing = self._payload()
            missing.pop("approval_reference")
            with self.assertRaisesRegex(SourceRightsManifestError, "missing=approval_reference"):
                load_source_rights_manifest(self._write(root, missing))

    def test_duplicate_json_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-rights.json"
            path.write_text(
                "{"
                '"schema_version":1,'
                '"schema_version":1,'
                '"kind":"autosport_source_rights_manifest",'
                '"source_identity":"source:x",'
                '"authorized_scopes":["historical.read"],'
                '"effective_at":"2026-09-01T00:00:00Z",'
                '"expires_at":"2026-12-01T00:00:00Z",'
                '"human_approved":true,'
                '"approved_by":"owner",'
                '"approval_reference":"record:1",'
                '"approved_at":"2026-08-31T00:00:00Z"'
                "}",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                SourceRightsManifestError,
                "duplicate JSON object key: schema_version",
            ):
                load_source_rights_manifest(path)

    def test_manifest_source_artifact_is_opened_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp))
            original_open = Path.open
            opens: list[Path] = []

            def tracked_open(target: Path, *args, **kwargs):
                opens.append(target)
                return original_open(target, *args, **kwargs)

            with patch.object(Path, "open", tracked_open):
                manifest = load_source_rights_manifest(path)

            self.assertEqual(opens, [path])
            self.assertEqual(
                manifest.manifest_sha256,
                hashlib.sha256(manifest.manifest_bytes).hexdigest(),
            )

    def test_post_load_path_replacement_cannot_expand_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self._write(root)
            manifest = load_source_rights_manifest(path)

            replacement = self._payload()
            replacement["authorized_scopes"] = [
                "historical.internal_research",
                "historical.read",
                "historical.redistribute",
            ]
            self._write(root, replacement)

            decision = authorize_source_use(
                manifest,
                source_identity=manifest.source_identity,
                required_scope="historical.read",
                at=datetime(2026, 9, 21, tzinfo=timezone.utc),
            )
            self.assertEqual(decision.required_scope, "historical.read")

            with self.assertRaisesRegex(
                SourceRightsManifestError,
                "required_scope is not explicitly authorized",
            ):
                authorize_source_use(
                    manifest,
                    source_identity=manifest.source_identity,
                    required_scope="historical.redistribute",
                    at=datetime(2026, 9, 21, tzinfo=timezone.utc),
                )

    def test_copied_manifest_fields_cannot_diverge_from_hash_bound_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = load_source_rights_manifest(self._write(Path(tmp)))
            forged = replace(
                manifest,
                source_identity="licensed-provider-account:other",
            )

            with self.assertRaisesRegex(
                SourceRightsManifestError,
                "snapshot fields are inconsistent",
            ):
                authorize_source_use(
                    forged,
                    source_identity="licensed-provider-account:other",
                    required_scope="historical.read",
                    at=datetime(2026, 9, 21, tzinfo=timezone.utc),
                )

    def test_authorization_check_requires_timezone_aware_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = load_source_rights_manifest(self._write(Path(tmp)))
            with self.assertRaisesRegex(
                SourceRightsManifestError,
                "authorization check time must be a built-in UTC datetime",
            ):
                authorize_source_use(
                    manifest,
                    source_identity=manifest.source_identity,
                    required_scope="historical.read",
                    at=datetime(2026, 9, 21),
                )


    def test_runtime_authority_graph_resists_coordinated_module_rebinding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp))
            manifest = load_source_rights_manifest(path)
            canonical_manifest_type = SourceRightsManifest
            canonical_authorization_type = SourceRightsAuthorization

            class ForgedPath:
                def __init__(self, *args, **kwargs) -> None:
                    raise AssertionError("forged Path must not execute")

            class ForgedManifest:
                pass

            class ForgedAuthorization:
                pass

            class ForgedHashlib:
                @staticmethod
                def sha256(payload: bytes) -> object:
                    raise AssertionError("forged sha256 must not execute")

            class ForgedJson:
                @staticmethod
                def loads(*args, **kwargs) -> object:
                    raise AssertionError("forged json.loads must not execute")

            forged_issuer = object()
            with (
                patch.object(source_rights_module, "Path", ForgedPath),
                patch.object(source_rights_module, "SourceRightsManifest", ForgedManifest),
                patch.object(
                    source_rights_module,
                    "SourceRightsAuthorization",
                    ForgedAuthorization,
                ),
                patch.object(source_rights_module, "hashlib", ForgedHashlib),
                patch.object(source_rights_module, "json", ForgedJson),
                patch.object(source_rights_module, "_AUTHORIZATION_ISSUER", forged_issuer),
                patch.object(
                    source_rights_module,
                    "_canonical_text",
                    lambda *args, **kwargs: "forged",
                ),
                patch.object(
                    source_rights_module,
                    "_runtime_timestamp",
                    lambda *args, **kwargs: datetime(2099, 1, 1, tzinfo=timezone.utc),
                ),
                patch.object(
                    source_rights_module,
                    "_validated_projection",
                    lambda *args, **kwargs: ("forged",),
                ),
                patch.object(
                    source_rights_module,
                    "_validated_manifest_snapshot",
                    lambda *args, **kwargs: ("forged",),
                ),
                patch.object(
                    source_rights_module,
                    "_bounded_manifest_bytes",
                    lambda value: b"forged",
                ),
                patch.object(
                    source_rights_module,
                    "_issue_source_rights_authorization",
                    lambda **kwargs: ForgedAuthorization(),
                ),
            ):
                loaded = load_source_rights_manifest(path)
                self.assertIs(type(loaded), canonical_manifest_type)
                self.assertEqual(loaded.manifest_bytes, manifest.manifest_bytes)
                self.assertEqual(loaded.manifest_sha256, manifest.manifest_sha256)

                decision = authorize_source_use(
                    manifest,
                    source_identity=manifest.source_identity,
                    required_scope="historical.read",
                    at=datetime(2026, 9, 21, tzinfo=timezone.utc),
                )
                self.assertIs(type(decision), canonical_authorization_type)
                self.assertEqual(decision.required_scope, "historical.read")
                self.assertEqual(decision.manifest_sha256, manifest.manifest_sha256)

                with self.assertRaisesRegex(
                    SourceRightsManifestError,
                    "can only be issued by authorize_source_use",
                ):
                    canonical_authorization_type(
                        source_identity=manifest.source_identity,
                        required_scope="historical.read",
                        checked_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
                        manifest_sha256=manifest.manifest_sha256,
                        approved_by=manifest.approved_by,
                        approval_reference=manifest.approval_reference,
                        _issuer=forged_issuer,
                    )


if __name__ == "__main__":
    unittest.main()
