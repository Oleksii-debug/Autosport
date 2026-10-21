"""Single fail-closed entry point for Windows update *preflight*, not install.

Composes immutable package snapshot, independent installed trust anchor,
canonical package verification, Ed25519 signature, and version/schema policy.
This function performs no install, download, rollback, migration or financial
operation; its return value is never a permission to execute a transaction.
"""

from __future__ import annotations

from pathlib import Path
import os

from .windows_update_installed_trust import (
    assess_update_with_installed_trust,
    load_pinned_installed_update_authority,
)
from .windows_update_admission import (
    WindowsUpdateCandidate, WindowsUpdateDecision, assess_windows_update,
    _assess_transition_without_signature, _sha, _schema,
)
from .windows_update_signature import (
    VerifiedWindowsUpdate, _signed_statement, _verify_signed_update_claim,
)
from .windows_update_local_path import require_local_update_path
from .windows_update_snapshot import pinned_windows_update_package, _MAX_PACKAGE_BYTES


class WindowsUpdatePreflightError(ValueError):
    """The proposed update lacks a complete verified admission chain."""


def _validate_static_inputs(
    *, package_path: str | Path, installed_manifest_path: str | Path,
    expected_package_sha256: str, expected_installed_manifest_sha256: str,
    signature: bytes, candidate_version: str, candidate_schema: int,
    expected_source_sha: str,
    supported_schema_migrations: tuple[tuple[int, int], ...],
    max_package_bytes: int | None,
) -> None:
    """Reject malformed operator input *before* package or manifest I/O.

    An otherwise invalid 64-byte signature or schema chain should not force
    copying and deep-verifying up to 2 GiB of untrusted ZIP data. This is
    structural admission only: it never authenticates the signature, manifest,
    package bytes, or the caller-supplied digest's independent provenance.
    """
    for path in (package_path, installed_manifest_path):
        if type(path) not in (str, type(Path())):
            raise WindowsUpdatePreflightError("update path is not canonical")
        require_local_update_path(os.fspath(path))
    _sha(expected_installed_manifest_sha256)
    # Reuse the exact signed-envelope validator for digest/source/version/
    # schema, rather than introducing another potentially divergent policy.
    _signed_statement(
        package_sha256=expected_package_sha256,
        source_sha=expected_source_sha,
        version=candidate_version,
        schema=candidate_schema,
    )
    if type(signature) is not bytes or len(signature) != 64:
        raise WindowsUpdatePreflightError("signature encoding is invalid")
    if max_package_bytes is not None and (
        type(max_package_bytes) is not int
        or not 1 <= max_package_bytes <= _MAX_PACKAGE_BYTES
    ):
        raise WindowsUpdatePreflightError("package size bound is invalid")
    if type(supported_schema_migrations) is not tuple or len(supported_schema_migrations) > 256:
        raise WindowsUpdatePreflightError("migration chain is not canonical")
    prior_end = None
    for edge in supported_schema_migrations:
        if type(edge) is not tuple or len(edge) != 2:
            raise WindowsUpdatePreflightError("migration edge is not canonical")
        start, end = edge
        _schema(start)
        _schema(end)
        if end != start + 1 or (prior_end is not None and start != prior_end):
            raise WindowsUpdatePreflightError("migration chain is not contiguous")
        prior_end = end


def preflight_signed_windows_update(
    *,
    package_path: str | Path,
    expected_package_sha256: str,
    installed_manifest_path: str | Path,
    expected_installed_manifest_sha256: str,
    signature: bytes,
    candidate_version: str,
    candidate_schema: int,
    expected_source_sha: str,
    supported_schema_migrations: tuple[tuple[int, int], ...],
    max_package_bytes: int | None = None,
) -> VerifiedWindowsUpdate:
    """Return non-authorizing, signed preflight evidence or fail closed.

    The expected package digest and installed manifest digest MUST be obtained
    from independent trusted sources. The installed manifest public key is not
    accepted as an independent root if its digest came from that same manifest.
    The caller MUST recheck both authorities within a separate future atomic
    installer transaction; this function intentionally cannot install.
    """
    try:
        _validate_static_inputs(
            package_path=package_path,
            installed_manifest_path=installed_manifest_path,
            expected_package_sha256=expected_package_sha256,
            expected_installed_manifest_sha256=expected_installed_manifest_sha256,
            signature=signature, candidate_version=candidate_version,
            candidate_schema=candidate_schema, expected_source_sha=expected_source_sha,
            supported_schema_migrations=supported_schema_migrations,
            max_package_bytes=max_package_bytes,
        )
        # Authenticate the tiny installed manifest and reject an impossible
        # version/schema transition before copying up to 2 GiB of package
        # bytes. This policy-only check does NOT claim a valid signature or
        # trust the caller's expected package digest as verified bytes.
        # The same canonical transition function is reused after Ed25519 and
        # deep package verification; no parallel progression rules exist.
        installed_before = load_pinned_installed_update_authority(
            installed_manifest_path,
            expected_manifest_sha256=expected_installed_manifest_sha256,
        )
        _assess_transition_without_signature(
            installed_before.installed,
            candidate_version=candidate_version,
            candidate_schema=candidate_schema,
            candidate_package_sha256=expected_package_sha256,
            supported_schema_migrations=supported_schema_migrations,
        )
        # The installed trust anchor supplies the only accepted public key.
        # Verify the claimed signed envelope *before* copying or decompressing
        # a candidate that may be up to 2 GiB. A signature PASS here is not
        # package evidence: the inner verifier will independently check exact
        # pinned bytes, embedded identity and the Ed25519 signature again.
        _verify_signed_update_claim(
            package_sha256=expected_package_sha256,
            source_sha=expected_source_sha,
            version=candidate_version, schema=candidate_schema,
            signature=signature,
            trusted_public_key=installed_before.trusted_public_key,
        )
        # The crypto provider is an external native dependency. If it changes
        # the installed manifest while verifying, do not copy any candidate
        # until the same independently pinned trust anchor is confirmed.
        installed_after_claim = load_pinned_installed_update_authority(
            installed_before._source_path,
            expected_manifest_sha256=expected_installed_manifest_sha256,
        )
        if installed_after_claim != installed_before:
            raise WindowsUpdatePreflightError("installed authority changed during signature admission")
        options = {} if max_package_bytes is None else {"max_bytes": max_package_bytes}
        with pinned_windows_update_package(
            package_path, expected_sha256=expected_package_sha256, **options
        ) as snapshot:
            # A frozen dataclass is not a security boundary: a backend can
            # replace every field using object.__setattr__, including its
            # path, digest and filesystem witnesses. Preserve the original
            # caller-approved snapshot identity outside that mutable object.
            pinned_candidate_witness = (
                snapshot.path, snapshot.sha256, snapshot.byte_count,
                snapshot._file_identity, snapshot._file_metadata,
                snapshot._change_time_100ns, snapshot._parent_identity,
            )
            if snapshot.sha256 != expected_package_sha256:
                raise WindowsUpdatePreflightError("candidate digest authority changed")
            # Pin the installed authority independently of the inner verifier.
            # A success-shaped backend receipt must not invent its FROM state.
            result = assess_update_with_installed_trust(
                installed_manifest_path,
                expected_manifest_sha256=expected_installed_manifest_sha256,
                snapshot=snapshot,
                candidate_version=candidate_version,
                candidate_schema=candidate_schema,
                expected_source_sha=expected_source_sha,
                signature=signature,
                supported_schema_migrations=supported_schema_migrations,
            )
            if (
                snapshot.path, snapshot.sha256, snapshot.byte_count,
                snapshot._file_identity, snapshot._file_metadata,
                snapshot._change_time_100ns, snapshot._parent_identity,
            ) != pinned_candidate_witness:
                raise WindowsUpdatePreflightError("candidate snapshot authority was rebound")
            installed_after = load_pinned_installed_update_authority(
                installed_before._source_path,
                expected_manifest_sha256=expected_installed_manifest_sha256,
            )
            if installed_before != installed_after:
                raise WindowsUpdatePreflightError("installed authority changed during preflight")
            # Check before the snapshot context exits as well as on its exit.
            snapshot.recheck()
            if type(result) is not VerifiedWindowsUpdate:
                raise WindowsUpdatePreflightError("untrusted update preflight result")
            # The backend must attest exactly the caller-pinned candidate,
            # not merely return any well-formed signed update receipt.
            # Revalidate even frozen dataclasses: object.__setattr__ can
            # mutate a previously constructed record.
            result.__post_init__()
            # Re-evaluate the canonical version/schema/migration policy using
            # the independently pinned installed identity, not the receipt's
            # self-reported FROM fields. This also binds NO_CHANGE to the
            # original installed package SHA, not just its version string.
            expected_decision = assess_windows_update(
                installed_before.installed,
                WindowsUpdateCandidate(
                    version=candidate_version,
                    schema=candidate_schema,
                    package_sha256=snapshot.sha256,
                    verified_package_sha256=snapshot.sha256,
                    signature_verified=True,
                ),
                supported_schema_migrations=supported_schema_migrations,
            )
            if result.decision != expected_decision:
                raise WindowsUpdatePreflightError("untrusted update preflight decision")
            if (
                result.package_sha256 != snapshot.sha256
                or result.source_sha != expected_source_sha
                or result.version != candidate_version
                or result.schema != candidate_schema
                or result.install_authorized is not False
                or result.decision.install_authorized is not False
            ):
                raise WindowsUpdatePreflightError("untrusted update preflight result")
            # The independent policy check runs after the inner verifier and
            # the earlier installed-manifest readbacks. Revalidate the original
            # pinned trust root once more: a concurrent same-byte inode swap
            # must not yield a stale success-shaped preflight receipt.
            installed_final = load_pinned_installed_update_authority(
                installed_before._source_path,
                expected_manifest_sha256=expected_installed_manifest_sha256,
            )
            if installed_final != installed_before:
                raise WindowsUpdatePreflightError("installed authority changed during final policy check")
            # The final installed-state read is itself a new I/O interval.
            # Recheck the private candidate snapshot before leaving the
            # context, which also independently rechecks on exit.
            snapshot.recheck()
            if (
                snapshot.path, snapshot.sha256, snapshot.byte_count,
                snapshot._file_identity, snapshot._file_metadata,
                snapshot._change_time_100ns, snapshot._parent_identity,
            ) != pinned_candidate_witness:
                raise WindowsUpdatePreflightError("candidate snapshot authority was rebound")
            # Do not return the inner verifier's receipt or decision object.
            # Frozen dataclasses can be mutated through object.__setattr__, and
            # a verifier retaining a reference can alter them during the last
            # snapshot recheck or context-manager cleanup *after* validation.
            # Rebuild the non-authorizing receipt from independent pinned facts
            # and the locally recomputed canonical policy instead.
            verified_package_sha = snapshot.sha256
            verified_decision = WindowsUpdateDecision(
                disposition=expected_decision.disposition,
                from_version=expected_decision.from_version,
                to_version=expected_decision.to_version,
                from_schema=expected_decision.from_schema,
                to_schema=expected_decision.to_schema,
            )
        return VerifiedWindowsUpdate(
            decision=verified_decision,
            package_sha256=verified_package_sha,
            source_sha=expected_source_sha,
            version=candidate_version,
            schema=candidate_schema,
        )
    except (
        ValueError, TypeError, AttributeError, KeyError, RuntimeError,
        OSError, UnicodeError, EOFError, ImportError, SyntaxError,
    ):
        # Do not leak installation paths, ZIP member names or backend traces
        # from any nested trust/crypto/package boundary to the operator.
        raise WindowsUpdatePreflightError("Windows update preflight rejected") from None
