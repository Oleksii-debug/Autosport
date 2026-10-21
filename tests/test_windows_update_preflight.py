"""Cross-boundary Windows update preflight: no installer and no real account."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from autosport.windows_update_preflight import preflight_signed_windows_update, WindowsUpdatePreflightError
from autosport.windows_update_signature import _signed_statement

SOURCE_SHA = "a" * 40


def _canonical_manifest(*, key: bytes, version="1.0.0", schema=1, package_sha="b" * 64):
    return (json.dumps({
        "format": "autosport-installed-update-authority-v1", "product": "Autosport",
        "version": version, "schema": schema, "package_sha256": package_sha,
        "trusted_ed25519_public_key_hex": key.hex(),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _case(tmp_path, *, new_version="1.0.1", new_schema=1):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    package = tmp_path / "candidate update.zip"
    package.write_bytes(b"package fixture bytes")
    package_sha = hashlib.sha256(package.read_bytes()).hexdigest()
    manifest = tmp_path / "інстальований стан.json"
    raw_manifest = _canonical_manifest(key=public)
    manifest.write_bytes(raw_manifest)
    signature = private.sign(_signed_statement(
        package_sha256=package_sha, source_sha=SOURCE_SHA,
        version=new_version, schema=new_schema,
    ))
    return {
        "package_path": package,
        "expected_package_sha256": package_sha,
        "installed_manifest_path": manifest,
        "expected_installed_manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
        "signature": signature,
        "candidate_version": new_version,
        "candidate_schema": new_schema,
        "expected_source_sha": SOURCE_SHA,
        "supported_schema_migrations": ((1, 2),) if new_schema == 2 else (),
    }


def _deep_verified(monkeypatch):
    from autosport import windows_update_signature
    called = []
    def canonical(snapshot, *, expected_source_sha):
        snapshot.recheck()
        called.append(snapshot.path)
        return {"status": "PASS", "source_sha": expected_source_sha,
                "package_sha256": snapshot.sha256,
                "update_version": "1.0.1", "update_schema": 1}
    monkeypatch.setattr(windows_update_signature, "verify_pinned_release_package", canonical)
    return called


def test_preflight_composes_snapshot_installed_trust_signature_and_policy(tmp_path, monkeypatch):
    called = _deep_verified(monkeypatch)
    inputs = _case(tmp_path)
    decision = preflight_signed_windows_update(**inputs)
    assert decision.decision.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE"
    assert decision.install_authorized is False
    assert decision.decision.install_authorized is False
    assert len(called) == 1
    assert not called[0].exists(), "temporary snapshot must be removed after preflight"
    assert preflight_signed_windows_update(**inputs).package_sha256 == decision.package_sha256


@pytest.mark.parametrize("mutation", ["candidate", "installed", "signature", "version", "schema", "size"])
def test_preflight_fails_closed_for_tampered_inputs(tmp_path, monkeypatch, mutation):
    _deep_verified(monkeypatch)
    inputs = _case(tmp_path)
    if mutation == "candidate":
        inputs["package_path"].write_bytes(b"different package")
    elif mutation == "installed":
        inputs["installed_manifest_path"].write_bytes(b"modified manifest")
    elif mutation == "signature":
        inputs["signature"] = b"\x00" * 64
    elif mutation == "version":
        inputs["candidate_version"] = "1.0.0"
    elif mutation == "schema":
        inputs["candidate_schema"] = 2
        inputs["supported_schema_migrations"] = ((1, 2),)
    elif mutation == "size":
        inputs["max_package_bytes"] = 1
    with pytest.raises(WindowsUpdatePreflightError):
        preflight_signed_windows_update(**inputs)


def test_preflight_never_accepts_installer_authority_or_credentials(tmp_path, monkeypatch):
    _deep_verified(monkeypatch)
    inputs = _case(tmp_path)
    with pytest.raises(TypeError):
        preflight_signed_windows_update(**inputs, install_authorized=True)
    with pytest.raises(TypeError):
        preflight_signed_windows_update(**inputs, bookmaker_token="not-permitted")


def test_unavailable_real_canonical_verifier_never_becomes_signature_only_pass(tmp_path):
    """Even a valid Ed25519 envelope cannot replace deep release verification."""
    inputs = _case(tmp_path)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        preflight_signed_windows_update(**inputs)


def test_snapshot_mutation_after_inner_assessment_is_rejected(tmp_path, monkeypatch):
    from autosport import windows_update_preflight as module
    inputs = _case(tmp_path)
    invoked = []

    def corrupt_after_inner_assessment(_manifest, *, snapshot, **_kwargs):
        invoked.append(True)
        snapshot.path.write_bytes(b"tampered after trust verification")
        # Even a fabricated success-shaped object must not bypass recheck.
        return object()

    monkeypatch.setattr(module, "assess_update_with_installed_trust", corrupt_after_inner_assessment)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        preflight_signed_windows_update(**inputs)
    assert invoked == [True]


@pytest.mark.parametrize("invalid", [True, 0, -1, "1024", 2**40])
def test_invalid_resource_bound_is_rejected_without_backend_call(tmp_path, monkeypatch, invalid):
    from autosport import windows_update_preflight as module
    inputs = _case(tmp_path)
    invoked = []
    monkeypatch.setattr(module, "assess_update_with_installed_trust", lambda *a, **k: invoked.append(1))
    with pytest.raises(WindowsUpdatePreflightError):
        preflight_signed_windows_update(**inputs, max_package_bytes=invalid)
    assert invoked == []


def test_invalid_signature_rejected_before_candidate_copy(tmp_path, monkeypatch):
    """An invalid signed envelope must not cause a 2-GiB candidate copy."""
    from autosport import windows_update_preflight as module
    inputs = _case(tmp_path)
    inputs["signature"] = b"\x00" * 64
    reached = []

    def unexpected_snapshot(*args, **kwargs):
        reached.append(True)
        raise AssertionError("candidate copy must not begin for a bad signature")

    monkeypatch.setattr(module, "pinned_windows_update_package", unexpected_snapshot)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        preflight_signed_windows_update(**inputs)
    assert reached == []


def test_wrong_trusted_key_rejected_before_candidate_copy(tmp_path, monkeypatch):
    """A correctly formed signature under another key is still cheap to deny."""
    from autosport import windows_update_preflight as module
    inputs = _case(tmp_path)
    wrong_key = Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    manifest_bytes = _canonical_manifest(key=wrong_key)
    inputs["installed_manifest_path"].write_bytes(manifest_bytes)
    inputs["expected_installed_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    reached = []

    def unexpected_snapshot(*args, **kwargs):
        reached.append(True)
        raise AssertionError("candidate copy must not begin for a wrong key")

    monkeypatch.setattr(module, "pinned_windows_update_package", unexpected_snapshot)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        preflight_signed_windows_update(**inputs)
    assert reached == []



def test_missing_early_ed25519_backend_never_copies_candidate(tmp_path, monkeypatch):
    """Provider unavailability is a fail-closed, pre-I/O rejection."""
    import builtins
    from autosport import windows_update_preflight as module
    inputs = _case(tmp_path)
    real_import = builtins.__import__
    reached = []

    def missing_crypto(name, *args, **kwargs):
        if name == "cryptography.exceptions":
            raise ImportError("private backend location must be redacted")
        return real_import(name, *args, **kwargs)

    def unexpected_snapshot(*args, **kwargs):
        reached.append(True)
        raise AssertionError("unavailable crypto must not copy candidate")

    monkeypatch.setattr(builtins, "__import__", missing_crypto)
    monkeypatch.setattr(module, "pinned_windows_update_package", unexpected_snapshot)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected") as exc:
        preflight_signed_windows_update(**inputs)
    assert reached == []
    assert "private backend location" not in str(exc.value)


def test_installed_manifest_mutation_during_early_crypto_never_copies_candidate(
    tmp_path, monkeypatch,
):
    """The trust root is read back after the external crypto boundary."""
    from autosport import windows_update_preflight as module
    inputs = _case(tmp_path)
    original = module._verify_signed_update_claim
    reached = []

    def mutate_trust(**kwargs):
        original(**kwargs)
        inputs["installed_manifest_path"].write_bytes(b"changed during crypto")

    def unexpected_snapshot(*args, **kwargs):
        reached.append(True)
        raise AssertionError("stale trust root must not copy candidate")

    monkeypatch.setattr(module, "_verify_signed_update_claim", mutate_trust)
    monkeypatch.setattr(module, "pinned_windows_update_package", unexpected_snapshot)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        preflight_signed_windows_update(**inputs)
    assert reached == []

def test_valid_signed_claim_still_requires_real_package_bytes(tmp_path):
    """An early signature PASS is never a package or installer PASS."""
    inputs = _case(tmp_path)
    inputs["package_path"].write_bytes(b"modified after the detached signature")
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        preflight_signed_windows_update(**inputs)


def test_temporary_snapshot_is_cleaned_after_invalid_signature(tmp_path, monkeypatch):
    """Invalid signature is denied without ever creating a private snapshot."""
    from autosport import windows_update_preflight as module
    inputs = _case(tmp_path)
    inputs["signature"] = b"\x00" * 64
    observed = []
    actual = module.assess_update_with_installed_trust

    def capture_snapshot(*args, snapshot, **kwargs):
        observed.append(snapshot.path)
        return actual(*args, snapshot=snapshot, **kwargs)

    monkeypatch.setattr(module, "assess_update_with_installed_trust", capture_snapshot)
    _deep_verified(monkeypatch)
    with pytest.raises(WindowsUpdatePreflightError):
        preflight_signed_windows_update(**inputs)
    assert observed == []

def test_rejected_update_remains_rejected_across_new_python_process(tmp_path):
    """Restart does not turn a digest-only package into an admitted update."""
    import os
    import subprocess
    import sys
    inputs = _case(tmp_path)
    inputs["expected_package_sha256"] = "0" * 64
    code = """
import sys
from autosport.windows_update_preflight import preflight_signed_windows_update, WindowsUpdatePreflightError
try:
    preflight_signed_windows_update(
        package_path=sys.argv[1], expected_package_sha256='0'*64,
        installed_manifest_path=sys.argv[2], expected_installed_manifest_sha256=sys.argv[3],
        signature=bytes.fromhex(sys.argv[4]), candidate_version='1.0.1',
        candidate_schema=1, expected_source_sha='a'*40, supported_schema_migrations=(),
    )
except WindowsUpdatePreflightError:
    print('DENIED')
else:
    print('UNSAFE_PASS')
    raise SystemExit(1)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    command = [sys.executable, "-c", code, str(inputs["package_path"]),
               str(inputs["installed_manifest_path"]),
               inputs["expected_installed_manifest_sha256"], inputs["signature"].hex()]
    for _ in range(2):
        run = subprocess.run(command, env=env, text=True, capture_output=True, timeout=15)
        assert run.returncode == 0
        assert run.stdout.strip() == "DENIED"
        assert run.stderr == ""


@pytest.mark.parametrize("mutation", [
    "package_sha256", "source_sha", "version", "schema",
    "decision_to_version", "decision_to_schema", "decision_disposition",
    "decision_install_authorized", "receipt_install_authorized",
])
def test_preflight_rejects_backend_receipt_with_mismatched_identity_or_authority(
    tmp_path, monkeypatch, mutation,
):
    """An internally inconsistent backend receipt is never a signed update PASS.

    The inner verifier is a dependency boundary; even a future refactor or
    compromised/incorrect adapter must not substitute another package,
    release identity, or installation permission after snapshot verification.
    """
    from autosport import windows_update_preflight as module
    from autosport.windows_update_admission import WindowsUpdateDecision
    from autosport.windows_update_signature import VerifiedWindowsUpdate

    inputs = _case(tmp_path)
    decision = WindowsUpdateDecision(
        "ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.0.1", 1, 1
    )
    receipt = VerifiedWindowsUpdate(
        decision, inputs["expected_package_sha256"], SOURCE_SHA, "1.0.1", 1
    )
    if mutation == "package_sha256":
        object.__setattr__(receipt, "package_sha256", "0" * 64)
    elif mutation == "source_sha":
        object.__setattr__(receipt, "source_sha", "f" * 40)
    elif mutation == "version":
        object.__setattr__(receipt, "version", "1.0.2")
    elif mutation == "schema":
        object.__setattr__(receipt, "schema", 2)
    elif mutation == "decision_to_version":
        object.__setattr__(decision, "to_version", "1.0.2")
    elif mutation == "decision_to_schema":
        object.__setattr__(decision, "to_schema", 2)
    elif mutation == "decision_disposition":
        object.__setattr__(decision, "disposition", "UNSAFE_PASS")
    elif mutation == "decision_install_authorized":
        object.__setattr__(decision, "install_authorized", True)
    elif mutation == "receipt_install_authorized":
        object.__setattr__(receipt, "install_authorized", True)
    monkeypatch.setattr(
        module, "assess_update_with_installed_trust", lambda *a, **k: receipt
    )
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        preflight_signed_windows_update(**inputs)


def test_signed_receipt_constructor_rejects_noncanonical_or_conflicting_decision():
    from autosport.windows_update_admission import WindowsUpdateDecision
    from autosport.windows_update_signature import VerifiedWindowsUpdate
    valid = WindowsUpdateDecision(
        "ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.0.1", 1, 1
    )
    forged = WindowsUpdateDecision(
        "ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.0.1", 1, 1
    )
    object.__setattr__(forged, "disposition", "UNKNOWN")
    candidates = [
        (object(), "1.0.1", 1),
        (forged, "1.0.1", 1),
        (valid, "1.0.2", 1),
        (valid, "1.0.1", 2),
    ]
    for decision, version, schema in candidates:
        with pytest.raises(ValueError):
            VerifiedWindowsUpdate(decision, "a" * 64, "b" * 40, version, schema)


def test_signed_no_change_receipt_is_canonical_but_non_authorizing():
    from autosport.windows_update_admission import WindowsUpdateDecision
    from autosport.windows_update_signature import VerifiedWindowsUpdate
    decision = WindowsUpdateDecision("NO_CHANGE", "1.0.1", "1.0.1", 2, 2)
    receipt = VerifiedWindowsUpdate(decision, "a" * 64, SOURCE_SHA, "1.0.1", 2)
    assert receipt.decision.disposition == "NO_CHANGE"
    assert receipt.install_authorized is False


@pytest.mark.parametrize("mutation", [
    "invalid_from_version", "invalid_from_schema", "downgrade_version",
    "downgrade_schema", "same_version_claimed_upgrade", "no_change_changed_schema",
])
def test_signed_receipt_rejects_incoherent_version_schema_transition(mutation):
    from autosport.windows_update_admission import WindowsUpdateDecision
    from autosport.windows_update_signature import VerifiedWindowsUpdate
    decision = WindowsUpdateDecision("ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.0.1", 1, 1)
    version, schema = "1.0.1", 1
    if mutation == "invalid_from_version":
        object.__setattr__(decision, "from_version", "01.0.0")
    elif mutation == "invalid_from_schema":
        object.__setattr__(decision, "from_schema", True)
    elif mutation == "downgrade_version":
        object.__setattr__(decision, "from_version", "1.0.2")
    elif mutation == "downgrade_schema":
        object.__setattr__(decision, "from_schema", 2)
    elif mutation == "same_version_claimed_upgrade":
        object.__setattr__(decision, "from_version", "1.0.1")
    elif mutation == "no_change_changed_schema":
        object.__setattr__(decision, "disposition", "NO_CHANGE")
        object.__setattr__(decision, "from_schema", 2)
    with pytest.raises(ValueError):
        VerifiedWindowsUpdate(decision, "a" * 64, SOURCE_SHA, version, schema)

@pytest.mark.parametrize("forgery", [
    "from_version", "from_schema", "false_no_change", "no_change_wrong_package",
])
def test_preflight_binds_backend_decision_to_pinned_installed_identity(
    tmp_path, monkeypatch, forgery,
):
    """A forged backend receipt cannot invent the installed pre-update state.

    Receipt self-consistency and candidate package SHA are insufficient:
    the transition's FROM side must match the separately pinned installed
    manifest, and NO_CHANGE must refer to the installed package itself.
    """
    from autosport import windows_update_preflight as module
    from autosport.windows_update_admission import WindowsUpdateDecision
    from autosport.windows_update_signature import VerifiedWindowsUpdate

    inputs = _case(tmp_path)
    if forgery == "from_version":
        decision = WindowsUpdateDecision(
            "ELIGIBLE_FOR_TRANSACTION_PREPARE", "0.9.9", "1.0.1", 1, 1
        )
    elif forgery == "from_schema":
        inputs["candidate_schema"] = 2
        inputs["supported_schema_migrations"] = ((1, 2),)
        decision = WindowsUpdateDecision(
            "ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.0.1", 2, 2
        )
    elif forgery == "false_no_change":
        decision = WindowsUpdateDecision("NO_CHANGE", "1.0.1", "1.0.1", 1, 1)
    else:
        inputs["candidate_version"] = "1.0.0"
        decision = WindowsUpdateDecision("NO_CHANGE", "1.0.0", "1.0.0", 1, 1)

    receipt = VerifiedWindowsUpdate(
        decision, inputs["expected_package_sha256"], SOURCE_SHA,
        inputs["candidate_version"], inputs["candidate_schema"],
    )
    monkeypatch.setattr(
        module, "assess_update_with_installed_trust", lambda *a, **kw: receipt
    )
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        preflight_signed_windows_update(**inputs)


def test_preflight_accepts_real_no_change_only_for_identical_installed_package(
    tmp_path, monkeypatch,
):
    """A valid NO_CHANGE must be byte-identical to installed state."""
    from autosport import windows_update_signature

    inputs = _case(tmp_path, new_version="1.0.0")
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    raw = _canonical_manifest(
        key=public, version="1.0.0", schema=1,
        package_sha=inputs["expected_package_sha256"],
    )
    inputs["installed_manifest_path"].write_bytes(raw)
    inputs["expected_installed_manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    inputs["signature"] = private.sign(_signed_statement(
        package_sha256=inputs["expected_package_sha256"],
        source_sha=SOURCE_SHA, version="1.0.0", schema=1,
    ))
    monkeypatch.setattr(
        windows_update_signature, "verify_pinned_release_package",
        lambda snapshot, *, expected_source_sha: {
            "status": "PASS", "update_version": "1.0.0", "update_schema": 1,
            "package_sha256": snapshot.sha256, "source_sha": expected_source_sha,
        },
    )
    receipt = preflight_signed_windows_update(**inputs)
    assert receipt.decision.disposition == "NO_CHANGE"
    assert receipt.install_authorized is False


def test_preflight_accepts_valid_signed_forward_schema_migration(tmp_path, monkeypatch):
    from autosport import windows_update_signature
    inputs = _case(tmp_path, new_schema=2)
    monkeypatch.setattr(
        windows_update_signature, "verify_pinned_release_package",
        lambda snapshot, *, expected_source_sha: {
            "status": "PASS", "update_version": "1.0.1", "update_schema": 2,
            "package_sha256": snapshot.sha256, "source_sha": expected_source_sha,
        },
    )
    receipt = preflight_signed_windows_update(**inputs)
    assert receipt.decision.from_schema == 1
    assert receipt.decision.to_schema == 2
    assert receipt.decision.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE"
    assert receipt.install_authorized is False


def test_preflight_rejects_installed_manifest_changed_by_backend(tmp_path, monkeypatch):
    from autosport import windows_update_preflight as module
    from autosport.windows_update_admission import WindowsUpdateDecision
    from autosport.windows_update_signature import VerifiedWindowsUpdate

    inputs = _case(tmp_path)
    receipt = VerifiedWindowsUpdate(
        WindowsUpdateDecision("ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.0.1", 1, 1),
        inputs["expected_package_sha256"], SOURCE_SHA, "1.0.1", 1,
    )
    def tamper_installed(*_args, **_kwargs):
        inputs["installed_manifest_path"].write_bytes(b"changed by concurrent updater")
        return receipt

    monkeypatch.setattr(module, "assess_update_with_installed_trust", tamper_installed)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected") as error:
        preflight_signed_windows_update(**inputs)
    assert str(inputs["installed_manifest_path"]) not in str(error.value)

@pytest.mark.parametrize("backend_error", ["unsupported", "internal"])
@pytest.mark.parametrize("failure_stage", ["key_import", "signature_verify"])
def test_preflight_crypto_backend_failure_is_sanitized_and_fail_closed(
    tmp_path, monkeypatch, backend_error, failure_stage,
):
    """Crypto-provider failure must not disclose paths or become a PASS receipt."""
    from cryptography.exceptions import InternalError, UnsupportedAlgorithm
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    inputs = _case(tmp_path)
    _deep_verified(monkeypatch)
    private_path = r"C:\\Users\\Private User\\secrets\\update.key"
    if backend_error == "unsupported":
        failure = UnsupportedAlgorithm(private_path)
    else:
        failure = InternalError(private_path, [])

    def broken_backend(_key):
        if failure_stage == "key_import":
            raise failure

        class BrokenVerifier:
            def verify(self, _signature, _statement):
                raise failure

        return BrokenVerifier()

    monkeypatch.setattr(Ed25519PublicKey, "from_public_bytes", broken_backend)
    with pytest.raises(WindowsUpdatePreflightError, match="^Windows update preflight rejected$") as captured:
        preflight_signed_windows_update(**inputs)
    assert private_path not in str(captured.value)
    assert captured.value.__cause__ is None


def test_preflight_rejects_installed_authority_mutation_during_final_policy_check(
    tmp_path, monkeypatch,
):
    """A verifier receipt cannot outlive a changed installed trust root.

    The independent policy comparison occurs after the inner trust checker has
    completed its own readbacks. A concurrent installed-state replacement at
    that boundary must not yield a success-shaped preflight receipt.
    """
    from autosport import windows_update_preflight as module

    _deep_verified(monkeypatch)
    inputs = _case(tmp_path)
    actual_policy = module.assess_windows_update
    invoked = []

    def change_installed_during_policy(installed, candidate, *, supported_schema_migrations):
        decision = actual_policy(
            installed, candidate, supported_schema_migrations=supported_schema_migrations,
        )
        invoked.append(True)
        inputs["installed_manifest_path"].write_bytes(b"replaced installed authority")
        return decision

    monkeypatch.setattr(module, "assess_windows_update", change_installed_during_policy)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        preflight_signed_windows_update(**inputs)
    assert invoked == [True]


def test_preflight_rejects_same_byte_installed_inode_swap_during_policy(
    tmp_path, monkeypatch,
):
    """A same-digest manifest replacement is not the original trust root."""
    import os
    from autosport import windows_update_preflight as module

    _deep_verified(monkeypatch)
    inputs = _case(tmp_path)
    actual_policy = module.assess_windows_update
    invoked = []

    def replace_installed_during_policy(installed, candidate, *, supported_schema_migrations):
        decision = actual_policy(
            installed, candidate, supported_schema_migrations=supported_schema_migrations,
        )
        manifest = inputs["installed_manifest_path"]
        replacement = tmp_path / "replacement installed authority.json"
        replacement.write_bytes(manifest.read_bytes())
        os.replace(replacement, manifest)
        invoked.append(True)
        return decision

    monkeypatch.setattr(module, "assess_windows_update", replace_installed_during_policy)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        preflight_signed_windows_update(**inputs)
    assert invoked == [True]


def test_preflight_final_installed_read_failure_does_not_leak_private_path(
    tmp_path, monkeypatch,
):
    """The new last read must retain the operator-safe failure boundary."""
    import traceback
    from autosport import windows_update_preflight as module

    _deep_verified(monkeypatch)
    inputs = _case(tmp_path)
    actual_load = module.load_pinned_installed_update_authority
    calls = []
    private_path = r"C:\Users\Private Owner\Autosport\secret-installed-manifest.json"

    def fail_final_read(*args, **kwargs):
        calls.append(True)
        if len(calls) == 3:
            raise OSError(private_path)
        return actual_load(*args, **kwargs)

    monkeypatch.setattr(module, "load_pinned_installed_update_authority", fail_final_read)
    with pytest.raises(WindowsUpdatePreflightError) as captured:
        preflight_signed_windows_update(**inputs)
    rendered = "".join(traceback.format_exception(captured.value))
    assert len(calls) == 3
    assert private_path not in rendered
    assert "preflight rejected" in rendered

@pytest.mark.parametrize("late_mutation", [
    "receipt_authority", "decision_authority", "package_digest", "decision_disposition",
])
def test_preflight_does_not_return_backend_receipt_mutated_during_final_snapshot_recheck(
    tmp_path, monkeypatch, late_mutation,
):
    """A late callback must not turn a verified preflight into a forged receipt.

    Even a frozen dataclass is mutable via object.__setattr__. The backend can
    retain its receipt and mutate it after the independent policy comparison,
    while the final snapshot I/O and context-manager cleanup still run.
    """
    from autosport import windows_update_preflight as module
    from autosport.windows_update_admission import WindowsUpdateDecision
    from autosport.windows_update_signature import VerifiedWindowsUpdate
    from autosport.windows_update_snapshot import WindowsUpdateSnapshot

    inputs = _case(tmp_path)
    backend_decision = WindowsUpdateDecision(
        "ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.0.1", 1, 1
    )
    backend_receipt = VerifiedWindowsUpdate(
        backend_decision, inputs["expected_package_sha256"], SOURCE_SHA, "1.0.1", 1
    )
    monkeypatch.setattr(
        module, "assess_update_with_installed_trust", lambda *a, **k: backend_receipt
    )
    original_policy = module.assess_windows_update
    after_policy = False
    changed = False

    def policy(installed, candidate, *, supported_schema_migrations):
        nonlocal after_policy
        decision = original_policy(
            installed, candidate, supported_schema_migrations=supported_schema_migrations
        )
        after_policy = True
        return decision

    original_recheck = WindowsUpdateSnapshot.recheck

    def recheck_and_mutate(self):
        nonlocal changed
        if after_policy and not changed:
            changed = True
            if late_mutation == "receipt_authority":
                object.__setattr__(backend_receipt, "install_authorized", True)
            elif late_mutation == "decision_authority":
                object.__setattr__(backend_decision, "install_authorized", True)
            elif late_mutation == "package_digest":
                object.__setattr__(backend_receipt, "package_sha256", "f" * 64)
            elif late_mutation == "decision_disposition":
                object.__setattr__(backend_decision, "disposition", "NO_CHANGE")
        return original_recheck(self)

    monkeypatch.setattr(module, "assess_windows_update", policy)
    monkeypatch.setattr(WindowsUpdateSnapshot, "recheck", recheck_and_mutate)
    receipt = preflight_signed_windows_update(**inputs)
    assert after_policy and changed
    assert receipt is not backend_receipt
    assert receipt.decision is not backend_decision
    assert receipt.install_authorized is False
    assert receipt.decision.install_authorized is False
    assert receipt.package_sha256 == inputs["expected_package_sha256"]
    assert receipt.decision.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE"


def test_preflight_returned_evidence_cannot_be_retroactively_changed_by_backend(
    tmp_path, monkeypatch,
):
    """After successful return, a retained backend object has no authority alias."""
    from autosport import windows_update_preflight as module
    from autosport.windows_update_admission import WindowsUpdateDecision
    from autosport.windows_update_signature import VerifiedWindowsUpdate

    inputs = _case(tmp_path)
    decision = WindowsUpdateDecision(
        "ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.0.1", 1, 1
    )
    backend = VerifiedWindowsUpdate(
        decision, inputs["expected_package_sha256"], SOURCE_SHA, "1.0.1", 1
    )
    monkeypatch.setattr(module, "assess_update_with_installed_trust", lambda *a, **k: backend)
    accepted = preflight_signed_windows_update(**inputs)
    object.__setattr__(backend, "install_authorized", True)
    object.__setattr__(backend, "package_sha256", "f" * 64)
    object.__setattr__(decision, "disposition", "NO_CHANGE")
    object.__setattr__(decision, "install_authorized", True)
    assert accepted is not backend
    assert accepted.decision is not decision
    assert accepted.install_authorized is False
    assert accepted.decision.install_authorized is False
    assert accepted.package_sha256 == inputs["expected_package_sha256"]
    assert accepted.decision.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE"


def test_preflight_rejects_backend_rebinding_entire_pinned_snapshot_authority(tmp_path, monkeypatch):
    """A mutable frozen snapshot must not replace the caller's expected SHA root.

    The inner verification boundary can use object.__setattr__ to forge all
    snapshot fields at once. A self-consistent replacement inode/digest is
    still NOT the independently approved candidate package.
    """
    from autosport import windows_update_preflight as module
    from autosport.windows_update_admission import WindowsUpdateDecision
    from autosport.windows_update_signature import VerifiedWindowsUpdate

    inputs = _case(tmp_path)
    expected = inputs["expected_package_sha256"]
    touched = []

    def replace_snapshot_authority(_manifest, *, snapshot, **_kwargs):
        replacement = snapshot.path.with_name("alternate.zip")
        replacement.write_bytes(b"different but internally consistent package")
        st = replacement.stat()
        digest = hashlib.sha256(replacement.read_bytes()).hexdigest()
        assert digest != expected
        object.__setattr__(snapshot, "path", replacement)
        object.__setattr__(snapshot, "sha256", digest)
        object.__setattr__(snapshot, "byte_count", st.st_size)
        object.__setattr__(snapshot, "_file_identity", (st.st_dev, st.st_ino))
        object.__setattr__(snapshot, "_file_metadata", (st.st_size, st.st_mtime_ns, st.st_ctime_ns))
        touched.append(replacement)
        return VerifiedWindowsUpdate(
            WindowsUpdateDecision("ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.0.1", 1, 1),
            digest, SOURCE_SHA, "1.0.1", 1,
        )

    monkeypatch.setattr(module, "assess_update_with_installed_trust", replace_snapshot_authority)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        preflight_signed_windows_update(**inputs)
    assert len(touched) == 1
    assert not touched[0].exists(), "both private snapshots must be removed"


def test_preflight_rejects_same_digest_replacement_of_snapshot_inode(tmp_path, monkeypatch):
    """Even a byte-identical new file is not the originally pinned object."""
    from autosport import windows_update_preflight as module
    from autosport.windows_update_admission import WindowsUpdateDecision
    from autosport.windows_update_signature import VerifiedWindowsUpdate

    inputs = _case(tmp_path)
    observed = []

    def replace_inode(_manifest, *, snapshot, **_kwargs):
        other = snapshot.path.with_name("same-content-other-inode.zip")
        other.write_bytes(snapshot.path.read_bytes())
        st = other.stat()
        object.__setattr__(snapshot, "path", other)
        object.__setattr__(snapshot, "_file_identity", (st.st_dev, st.st_ino))
        object.__setattr__(snapshot, "_file_metadata", (st.st_size, st.st_mtime_ns, st.st_ctime_ns))
        observed.append(other)
        return VerifiedWindowsUpdate(
            WindowsUpdateDecision("ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.0.1", 1, 1),
            inputs["expected_package_sha256"], SOURCE_SHA, "1.0.1", 1,
        )

    monkeypatch.setattr(module, "assess_update_with_installed_trust", replace_inode)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        preflight_signed_windows_update(**inputs)
    assert len(observed) == 1
    assert not observed[0].exists()

@pytest.mark.parametrize("field, invalid", [
    ("signature", b"short"),
    ("signature", bytearray(b"0" * 64)),
    ("candidate_version", "2147483648.0.0"),
    ("candidate_version", "01.0.0"),
    ("candidate_schema", True),
    ("candidate_schema", 0),
    ("expected_source_sha", "Z" * 40),
    ("expected_package_sha256", "0" * 63),
    ("expected_installed_manifest_sha256", "0" * 63),
    ("supported_schema_migrations", [(1, 2)]),
    ("supported_schema_migrations", ((1, 3),)),
    ("supported_schema_migrations", ((1, 2), (3, 4))),
    ("supported_schema_migrations", ((1, True),)),
    ("supported_schema_migrations", ((2**31 - 1, 2**31),)),
    ("max_package_bytes", 0),
    ("max_package_bytes", True),
])
def test_malformed_static_update_admission_rejected_before_package_io(
    tmp_path, monkeypatch, field, invalid,
):
    """Cheap untrusted scalars must not trigger a multi-gigabyte snapshot copy.

    An invalid signed envelope, resource bound, or migration shape can be
    rejected before reading any package bytes or touching the installed root.
    """
    from autosport import windows_update_preflight as module
    inputs = _case(tmp_path)
    inputs[field] = invalid
    package_before = inputs["package_path"].read_bytes()
    manifest_before = inputs["installed_manifest_path"].read_bytes()
    observed = []

    def forbidden_snapshot(*args, **kwargs):
        observed.append("package_io")
        raise AssertionError("untrusted input reached package I/O")

    monkeypatch.setattr(module, "pinned_windows_update_package", forbidden_snapshot)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        module.preflight_signed_windows_update(**inputs)
    assert observed == []
    assert inputs["package_path"].read_bytes() == package_before
    assert inputs["installed_manifest_path"].read_bytes() == manifest_before


def test_canonical_static_update_admission_still_reaches_pinned_snapshot(tmp_path, monkeypatch):
    """The early gate is fail-fast, not a replacement for real package proof."""
    from autosport import windows_update_preflight as module
    inputs = _case(tmp_path)
    observed = []

    def stopped_snapshot(*args, **kwargs):
        observed.append((args, kwargs))
        raise OSError("private path must be redacted")

    monkeypatch.setattr(module, "pinned_windows_update_package", stopped_snapshot)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected") as error:
        module.preflight_signed_windows_update(**inputs)
    assert len(observed) == 1
    assert "private path" not in str(error.value)

@pytest.mark.parametrize("field, invalid", [
    ("package_path", r"\\server\share\candidate.zip"),
    ("installed_manifest_path", r"\\?\C:\secrets\manifest.json"),
    ("package_path", "candidate/../other.zip"),
    ("installed_manifest_path", "manifest.json:alternate"),
    ("package_path", b"candidate.zip"),
    ("installed_manifest_path", False),
])
def test_preflight_invalid_direct_call_paths_never_open_package(
    tmp_path, monkeypatch, field, invalid,
):
    from autosport import windows_update_preflight as module
    inputs = _case(tmp_path)
    package_before = inputs["package_path"].read_bytes()
    manifest_before = inputs["installed_manifest_path"].read_bytes()
    inputs[field] = invalid
    opened = []

    def forbidden(*args, **kwargs):
        opened.append(True)
        raise AssertionError("I/O reached")

    monkeypatch.setattr(module, "pinned_windows_update_package", forbidden)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        module.preflight_signed_windows_update(**inputs)
    assert opened == []
    # The original files remain intact even when the direct caller is hostile.
    if field == "package_path":
        assert (tmp_path / "candidate update.zip").read_bytes() == package_before
    else:
        assert (tmp_path / "інстальований стан.json").read_bytes() == manifest_before


def test_static_admission_accepts_valid_maximum_migration_shape_before_real_verification(
    tmp_path, monkeypatch,
):
    """The early guard must not impose an arbitrary shorter migration limit."""
    from autosport import windows_update_preflight as module
    inputs = _case(tmp_path)
    inputs["candidate_schema"] = 257
    inputs["supported_schema_migrations"] = tuple((n, n + 1) for n in range(1, 257))
    # The early crypto gate now precedes candidate I/O. Keep the envelope
    # genuinely signed for schema 257 so this still tests the upper bound.
    signer = Ed25519PrivateKey.generate()
    key = signer.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    installed_bytes = _canonical_manifest(key=key)
    inputs["installed_manifest_path"].write_bytes(installed_bytes)
    inputs["expected_installed_manifest_sha256"] = hashlib.sha256(installed_bytes).hexdigest()
    inputs["signature"] = signer.sign(_signed_statement(
        package_sha256=inputs["expected_package_sha256"],
        source_sha=inputs["expected_source_sha"],
        version=inputs["candidate_version"], schema=257,
    ))
    observed = []

    def stopped(*args, **kwargs):
        observed.append(True)
        raise OSError("private path")

    monkeypatch.setattr(module, "pinned_windows_update_package", stopped)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        module.preflight_signed_windows_update(**inputs)
    assert observed == [True]


@pytest.mark.parametrize("mutation", [
    "version_downgrade", "schema_downgrade", "same_version_repack",
    "same_version_migration", "wrong_start", "missing_edge", "extra_edge",
])
def test_installed_transition_policy_rejects_before_candidate_copy(
    tmp_path, monkeypatch, mutation,
):
    """A tiny pinned installed root must gate expensive untrusted ZIP I/O.

    The request's SHA is *not* accepted as signed evidence: this test only
    proves that an impossible transition cannot force a 2-GiB snapshot copy.
    """
    from autosport import windows_update_preflight as module
    inputs = _case(tmp_path, new_version="2.0.0", new_schema=3)
    inputs["supported_schema_migrations"] = ((1, 2), (2, 3))
    if mutation == "version_downgrade":
        inputs["candidate_version"] = "0.9.9"
    elif mutation == "schema_downgrade":
        inputs["candidate_schema"] = 0  # rejected by static check
    elif mutation == "same_version_repack":
        inputs["candidate_version"] = "1.0.0"
        inputs["candidate_schema"] = 1
        inputs["supported_schema_migrations"] = ()
    elif mutation == "same_version_migration":
        inputs["candidate_version"] = "1.0.0"
    elif mutation == "wrong_start":
        inputs["supported_schema_migrations"] = ((2, 3), (3, 4))
    elif mutation == "missing_edge":
        inputs["supported_schema_migrations"] = ((1, 2),)
    elif mutation == "extra_edge":
        inputs["supported_schema_migrations"] = ((1, 2), (2, 3), (3, 4))
    touched = []

    def forbidden(*args, **kwargs):
        touched.append(True)
        raise AssertionError("untrusted candidate bytes reached snapshot")

    monkeypatch.setattr(module, "pinned_windows_update_package", forbidden)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        module.preflight_signed_windows_update(**inputs)
    assert touched == []


def test_corrupt_installed_root_rejected_before_candidate_copy(tmp_path, monkeypatch):
    from autosport import windows_update_preflight as module
    inputs = _case(tmp_path)
    inputs["installed_manifest_path"].write_bytes(b"corrupted root")
    monkeypatch.setattr(module, "pinned_windows_update_package", lambda *a, **k: pytest.fail(
        "candidate snapshot started without installed trust"
    ))
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        module.preflight_signed_windows_update(**inputs)


def test_installed_root_swapped_after_early_policy_check_is_not_accepted(
    tmp_path, monkeypatch,
):
    """Early policy qualification is not a replacement for final trust readback."""
    import os
    from contextlib import contextmanager
    from autosport import windows_update_preflight as module
    inputs = _case(tmp_path)
    _deep_verified(monkeypatch)
    actual_snapshot = module.pinned_windows_update_package
    manifest = inputs["installed_manifest_path"]
    swapped = []

    @contextmanager
    def change_root_after_policy(*args, **kwargs):
        # The early pinned manifest has already been read at this point.
        replacement = tmp_path / "replacement manifest.json"
        replacement.write_bytes(manifest.read_bytes())
        os.replace(replacement, manifest)
        swapped.append(True)
        with actual_snapshot(*args, **kwargs) as snapshot:
            yield snapshot

    monkeypatch.setattr(module, "pinned_windows_update_package", change_root_after_policy)
    with pytest.raises(WindowsUpdatePreflightError, match="preflight rejected"):
        module.preflight_signed_windows_update(**inputs)
    assert swapped == [True]
    monkeypatch.setattr(module, "pinned_windows_update_package", actual_snapshot)
    # A *new* preflight may pin the now-stable root; stale identity is never
    # carried over to a fresh operation after a recoverable local replacement.
    assert module.preflight_signed_windows_update(**inputs).install_authorized is False
