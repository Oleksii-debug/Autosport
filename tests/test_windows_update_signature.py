"""Real Ed25519 fixture verification; no production key or private secret saved."""

import hashlib

import pytest

crypto = pytest.importorskip("cryptography", reason="optional Ed25519 verification extra")
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from autosport.windows_update_admission import InstalledWindowsProduct
from autosport.windows_update_signature import (
    WindowsUpdateSignatureError,
    _signed_statement,
    assess_signed_windows_update,
)
from autosport.windows_update_snapshot import pinned_windows_update_package


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def signed_fixture(tmp_path, monkeypatch):
    from autosport import windows_update_signature as module
    package = tmp_path / "оновлення з пробілами.zip"
    package.write_bytes(b"canonical ZIP bytes fixture")
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    source_sha = "a" * 40
    version = "1.2.0"
    schema = 2
    statement = _signed_statement(
        package_sha256=digest(package.read_bytes()), source_sha=source_sha,
        version=version, schema=schema,
    )
    signature = key.sign(statement)
    monkeypatch.setattr(module, "verify_pinned_release_package", lambda snapshot, *, expected_source_sha: {
        "status": "PASS", "package_sha256": snapshot.sha256, "source_sha": expected_source_sha,
        "update_version": version, "update_schema": schema,
    })
    installed = InstalledWindowsProduct("1.1.0", 1, digest(b"old"))
    return package, installed, public_key, signature, source_sha, version, schema


def evaluate(fixture, **overrides):
    package, installed, public_key, signature, source_sha, version, schema = fixture
    options = dict(candidate_version=version, candidate_schema=schema,
                   expected_source_sha=source_sha, signature=signature,
                   trusted_public_key=public_key,
                   supported_schema_migrations=((1, 2),))
    options.update(overrides)
    with pinned_windows_update_package(package, expected_sha256=digest(package.read_bytes())) as snap:
        return assess_signed_windows_update(installed, snap, **options)


def test_valid_signed_update_is_preflight_only(signed_fixture):
    result = evaluate(signed_fixture)
    assert result.decision.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE"
    assert result.install_authorized is False
    assert result.decision.install_authorized is False


@pytest.mark.parametrize("changes", [
    {"candidate_version": "1.2.1"},
    {"candidate_schema": 3},
    {"expected_source_sha": "b" * 40},
    {"signature": b"\x00" * 64},
    {"trusted_public_key": b"\x00" * 32},
])
def test_signature_covers_version_schema_source_and_key(signed_fixture, changes):
    with pytest.raises(WindowsUpdateSignatureError):
        evaluate(signed_fixture, **changes)


def test_signed_downgrade_still_rejected(signed_fixture, monkeypatch):
    package, installed, public_key, _, source_sha, _, schema = signed_fixture
    # Signing is necessary but never overrides monotonic installed-version policy.
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    signature = key.sign(_signed_statement(
        package_sha256=digest(package.read_bytes()), source_sha=source_sha,
        version="1.0.0", schema=schema,
    ))
    from autosport import windows_update_signature as module
    monkeypatch.setattr(module, "verify_pinned_release_package", lambda snap, *, expected_source_sha: {
        "status": "PASS", "package_sha256": snap.sha256,
        "source_sha": expected_source_sha, "update_version": "1.0.0", "update_schema": schema,
    })
    with pytest.raises(WindowsUpdateSignatureError, match="policy admission"):
        evaluate(signed_fixture, candidate_version="1.0.0", signature=signature,
                 trusted_public_key=public_key)


def test_signed_schema_downgrade_still_rejected(signed_fixture, monkeypatch):
    package, _, _, _, source_sha, version, _ = signed_fixture
    installed = InstalledWindowsProduct("1.1.0", 2, digest(b"old"))
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    signature = key.sign(_signed_statement(
        package_sha256=digest(package.read_bytes()), source_sha=source_sha,
        version=version, schema=1,
    ))
    from autosport import windows_update_signature as module
    monkeypatch.setattr(module, "verify_pinned_release_package", lambda snap, *, expected_source_sha: {
        "status": "PASS", "package_sha256": snap.sha256,
        "source_sha": expected_source_sha, "update_version": version, "update_schema": 1,
    })
    with pinned_windows_update_package(package, expected_sha256=digest(package.read_bytes())) as snap:
        with pytest.raises(WindowsUpdateSignatureError, match="policy admission"):
            assess_signed_windows_update(
                installed, snap, candidate_version=version, candidate_schema=1,
                expected_source_sha=source_sha, signature=signature,
                trusted_public_key=public_key, supported_schema_migrations=(),
            )


def test_signed_bytes_changed_after_signature_are_rejected(signed_fixture):
    package, *_ = signed_fixture
    package.write_bytes(b"different candidate bytes")
    with pytest.raises(WindowsUpdateSignatureError, match="signature rejected"):
        evaluate(signed_fixture)


def test_signature_backend_absence_is_fail_closed(signed_fixture, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_crypto(name, *args, **kwargs):
        if name.startswith("cryptography.exceptions"):
            raise ImportError("simulated absent crypto dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_crypto)
    with pytest.raises(WindowsUpdateSignatureError, match="backend unavailable"):
        evaluate(signed_fixture)


def test_signature_cannot_cover_modified_snapshot(signed_fixture, monkeypatch):
    from autosport import windows_update_signature as module
    package, installed, public_key, signature, source_sha, version, schema = signed_fixture
    def mutating_verifier(snapshot, *, expected_source_sha):
        claimed_digest = snapshot.sha256
        snapshot.path.write_bytes(b"tampered bytes")
        return {"status": "PASS", "package_sha256": claimed_digest,
                "source_sha": expected_source_sha,
                "update_version": version, "update_schema": schema}
    monkeypatch.setattr(module, "verify_pinned_release_package", mutating_verifier)
    with pytest.raises(WindowsUpdateSignatureError, match="integrity"):
        evaluate(signed_fixture)


def test_noncanonical_signature_inputs_fail_closed(signed_fixture):
    for change in ({"signature": bytearray(b"\0" * 64)},
                   {"trusted_public_key": bytearray(b"\0" * 32)},
                   {"candidate_version": "01.2.0"},
                   {"expected_source_sha": "A" * 40},
                   {"candidate_schema": True}):
        with pytest.raises(WindowsUpdateSignatureError):
            evaluate(signed_fixture, **change)


def test_signed_package_metadata_must_bind_claimed_version_and_schema(signed_fixture, monkeypatch):
    """A valid signature cannot upgrade an older payload by relabeling its envelope."""
    from autosport import windows_update_signature as module

    package, installed, public_key, signature, source_sha, version, schema = signed_fixture
    # The current canonical release verifier does not expose update version/schema.
    # A signature alone cannot authorize this ambiguous legacy package.
    monkeypatch.setattr(module, "verify_pinned_release_package", lambda *a, **k: {
        "status": "PASS", "package_sha256": digest(package.read_bytes()),
        "source_sha": source_sha,
    })
    with pytest.raises(WindowsUpdateSignatureError, match="embedded update identity"):
        evaluate(signed_fixture)


@pytest.mark.parametrize("reported", [
    {"update_version": "1.1.0", "update_schema": 2},
    {"update_version": "1.2.0", "update_schema": 1},
    {"update_version": "1.2.0", "update_schema": True},
    {"update_version": "1.2.0", "update_schema": "2"},
    {"update_version": "01.2.0", "update_schema": 2},
])
def test_signed_package_embedded_identity_mismatch_fails(signed_fixture, monkeypatch, reported):
    from autosport import windows_update_signature as module
    package, installed, public_key, signature, source_sha, version, schema = signed_fixture
    monkeypatch.setattr(module, "verify_pinned_release_package", lambda *a, **k: {
        "status": "PASS", "package_sha256": digest(package.read_bytes()),
        "source_sha": source_sha, **reported,
    })
    with pytest.raises(WindowsUpdateSignatureError, match="embedded update identity"):
        evaluate(signed_fixture)


def test_actual_zip_metadata_and_signature_form_one_fail_closed_preflight(tmp_path, monkeypatch):
    """No mocked metadata report: signed identity is read from pinned ZIP bytes."""
    import json
    import sys
    import types
    import zipfile
    from autosport import windows_update_snapshot as snapshot_module

    key = Ed25519PrivateKey.generate()
    trusted_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    source_sha = "a" * 40
    package = tmp_path / "оновлення без прав адміністратора.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", json.dumps({
            "product": "Autosport", "source_sha": source_sha,
            "version": "1.2.0", "update_version": "1.2.0", "update_schema": 2,
        }))
    package_sha = digest(package.read_bytes())
    signature = key.sign(_signed_statement(
        package_sha256=package_sha, source_sha=source_sha,
        version="1.2.0", schema=2,
    ))
    # Only canonical ZIP deep verification is stubbed here; the real snapshot,
    # embedded identity extraction, Ed25519, and downgrade admission all run.
    fake_release = types.ModuleType("autosport.release_package")
    fake_release.verify_windows_package = lambda path, *, expected_source_sha: {
        "status": "PASS", "package_sha256": digest(path.read_bytes()),
        "source_sha": expected_source_sha,
    }
    monkeypatch.setitem(sys.modules, "autosport.release_package", fake_release)
    installed = InstalledWindowsProduct("1.1.0", 1, digest(b"old"))
    with pinned_windows_update_package(package, expected_sha256=package_sha) as snap:
        result = assess_signed_windows_update(
            installed, snap, candidate_version="1.2.0", candidate_schema=2,
            expected_source_sha=source_sha, signature=signature,
            trusted_public_key=trusted_key, supported_schema_migrations=((1, 2),),
        )
        assert result.decision.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE"
        assert result.install_authorized is False
        assert result.decision.install_authorized is False

        # The early envelope check rejects a version relabel before deep ZIP
        # parsing; the embedded-identity check still gates valid signatures.
        with pytest.raises(WindowsUpdateSignatureError, match="detached update signature rejected"):
            assess_signed_windows_update(
                installed, snap, candidate_version="1.3.0", candidate_schema=2,
                expected_source_sha=source_sha, signature=signature,
                trusted_public_key=trusted_key, supported_schema_migrations=((1, 2),),
            )

@pytest.mark.parametrize("fault_stage", ["missing_backend", "package_verifier"])
def test_direct_signed_preflight_redacts_nested_private_exception_traceback(
    signed_fixture, monkeypatch, fault_stage
):
    """Public signed preflight must not leak backend paths via exception chaining."""
    import builtins
    import traceback
    from autosport import windows_update_signature as module

    private_path = r"C:\Users\Private Operator\secret-install\update.key"
    if fault_stage == "missing_backend":
        real_import = builtins.__import__

        def missing_backend(name, *args, **kwargs):
            if name == "cryptography.exceptions":
                raise ImportError(f"could not import from {private_path}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", missing_backend)
    else:
        from autosport.windows_update_snapshot import WindowsUpdateSnapshotError

        def failing_verifier(*args, **kwargs):
            raise WindowsUpdateSnapshotError(f"cannot read {private_path}")

        monkeypatch.setattr(module, "verify_pinned_release_package", failing_verifier)

    with pytest.raises(WindowsUpdateSignatureError) as failure:
        evaluate(signed_fixture)
    rendered = "".join(traceback.format_exception(failure.value))
    assert private_path not in rendered
    assert failure.value.__cause__ is None

@pytest.mark.parametrize("phase", ["load_key", "verify_signature"])
@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
def test_direct_signed_preflight_redacts_native_crypto_provider_failures(
    signed_fixture, monkeypatch, phase, error_type,
):
    """Native backend faults are denied without leaking an installed-user path.

    This tests the public signature API independently of the higher-level
    preflight wrapper, which must not be the only exception-safety boundary.
    """
    import traceback
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    private_path = r"C:\Users\Private Operator\Autosport\update.key"

    def fail(*_args, **_kwargs):
        raise error_type(f"backend failed at {private_path}")

    class FaultingVerifier:
        def verify(self, *_args, **_kwargs):
            fail()

    if phase == "load_key":
        monkeypatch.setattr(Ed25519PublicKey, "from_public_bytes", staticmethod(fail))
    else:
        monkeypatch.setattr(
            Ed25519PublicKey, "from_public_bytes", staticmethod(lambda *_: FaultingVerifier())
        )

    with pytest.raises(WindowsUpdateSignatureError, match="backend unavailable") as failure:
        evaluate(signed_fixture)
    rendered = "".join(traceback.format_exception(failure.value))
    assert private_path not in rendered
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is not None
    assert failure.value.__suppress_context__ is True

@pytest.mark.parametrize("error_type", [OSError, RuntimeError, SyntaxError])
def test_direct_signed_preflight_redacts_crypto_import_runtime_faults(
    signed_fixture, monkeypatch, error_type,
):
    """A broken crypto dependency cannot expose a local path to callers."""
    import builtins
    import traceback

    original_import = builtins.__import__
    private_path = r"C:\Users\Private Operator\Autosport\backend.pyd"

    def failing_import(name, *args, **kwargs):
        if name == "cryptography.exceptions":
            raise error_type(f"failed to import {private_path}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    with pytest.raises(WindowsUpdateSignatureError, match="backend unavailable") as failure:
        evaluate(signed_fixture)
    rendered = "".join(traceback.format_exception(failure.value))
    assert private_path not in rendered
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__ is True

@pytest.mark.parametrize("field,spoofed", [
    ("status", "FAIL"),
    ("status", True),
    ("package_sha256", "0" * 64),
    ("package_sha256", None),
    ("source_sha", "b" * 40),
    ("source_sha", None),
])
def test_signed_update_rejects_inconsistent_deep_verifier_receipt(
    signed_fixture, monkeypatch, field, spoofed,
):
    """The signature layer must reject forged backend PASS evidence itself.

    A future verifier refactor or integration adapter must not be able to
    return a receipt for different bytes, source, or failure status while
    still passing a cryptographically valid envelope signature.
    """
    from autosport import windows_update_signature as module
    package, _, _, _, source_sha, version, schema = signed_fixture
    report = {
        "status": "PASS", "package_sha256": digest(package.read_bytes()),
        "source_sha": source_sha, "update_version": version,
        "update_schema": schema,
    }
    report[field] = spoofed
    monkeypatch.setattr(module, "verify_pinned_release_package", lambda *a, **k: report)
    with pytest.raises(WindowsUpdateSignatureError, match="verifier"):
        evaluate(signed_fixture)


@pytest.mark.parametrize("version", [
    "2147483648.0.0", "0.2147483648.0", "0.0.2147483648",
])
def test_signed_statement_rejects_versions_outside_release_policy(version):
    """A signer must not produce statements rejected by the release writer."""
    with pytest.raises(WindowsUpdateSignatureError, match="version must be canonical"):
        _signed_statement(
            package_sha256="a" * 64, source_sha="b" * 40,
            version=version, schema=1,
        )


def test_signed_statement_accepts_release_policy_maximum_version_and_schema():
    """The signed envelope and release writer share one inclusive upper bound."""
    from autosport.windows_update_release_metadata import validate_requested_update_identity

    maximum = "2147483647.2147483647.2147483647"
    validate_requested_update_identity(update_version=maximum, update_schema=2**31 - 1)
    statement = _signed_statement(
        package_sha256="a" * 64, source_sha="b" * 40,
        version=maximum, schema=2**31 - 1,
    )
    assert b'"version":"2147483647.2147483647.2147483647"' in statement
    assert b'"schema":2147483647' in statement


@pytest.mark.parametrize("failure", ["bad_signature", "wrong_installed_key"])
def test_direct_signed_assessor_rejects_bad_envelope_before_deep_zip(
    signed_fixture, monkeypatch, failure,
):
    """Direct callers cannot spend deep-verifier resources on bad signatures."""
    from autosport import windows_update_signature as module
    package, installed, public_key, signature, source_sha, version, schema = signed_fixture
    if failure == "bad_signature":
        signature = b"\x00" * 64
    else:
        signer = Ed25519PrivateKey.generate()
        public_key = signer.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    reached = []

    def unexpected_deep_verification(*args, **kwargs):
        reached.append(True)
        raise AssertionError("bad signature must not trigger deep ZIP parsing")

    monkeypatch.setattr(module, "verify_pinned_release_package", unexpected_deep_verification)
    with pinned_windows_update_package(package, expected_sha256=digest(package.read_bytes())) as snap:
        with pytest.raises(WindowsUpdateSignatureError):
            assess_signed_windows_update(
                installed, snap, candidate_version=version, candidate_schema=schema,
                expected_source_sha=source_sha, signature=signature,
                trusted_public_key=public_key,
                supported_schema_migrations=((1, 2),),
            )
    assert reached == []
