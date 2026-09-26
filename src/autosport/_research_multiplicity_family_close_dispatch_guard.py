"""Seal family-close evidence publication against mutable module dispatch.

The owning module already re-resolves the exact canonical multiplicity store and
pins its private read/enrollment dispatch. The final evidence constructors,
serializers and digest helpers are also authority-bearing: rebinding one while the
same module later performs ``require_current_*`` must not turn caller code into
family-close authority.

The canonical store constructor is authority-bearing too. Family-close
re-resolution must fail closed if class-level constructor dispatch changes while
keeping the exact store type/read/enrollment functions.

This guard adds no promotion authority. ``promotion_authorized`` remains false.
"""

from __future__ import annotations

from . import research_multiplicity_family_close as _close


def _install_guard() -> None:
    error_type = _close.MultiplicityFamilyCloseError
    derive_name = _close.derive_multiplicity_family_close.__name__
    derive_qualname = _close.derive_multiplicity_family_close.__qualname__
    derive_doc = _close.derive_multiplicity_family_close.__doc__
    derive_module = _close.derive_multiplicity_family_close.__module__
    require_current_name = _close.require_current_multiplicity_family_close.__name__
    require_current_qualname = _close.require_current_multiplicity_family_close.__qualname__
    require_current_doc = _close.require_current_multiplicity_family_close.__doc__
    require_current_module = _close.require_current_multiplicity_family_close.__module__

    member_type = _close.TerminalMultiplicityMemberEvidence
    member_init = member_type.__init__
    member_init_code = getattr(member_init, "__code__", None)
    member_post_init = member_type.__post_init__
    member_post_init_code = getattr(member_post_init, "__code__", None)
    member_to_payload = member_type.to_payload
    member_to_payload_code = getattr(member_to_payload, "__code__", None)
    member_eq = member_type.__eq__
    member_eq_code = getattr(member_eq, "__code__", None)
    member_fields = tuple(
        (name, getattr(member_type, name))
        for name in member_type.__dataclass_fields__
    )

    evidence_type = _close.MultiplicityFamilyCloseEvidence
    evidence_init = evidence_type.__init__
    evidence_init_code = getattr(evidence_init, "__code__", None)
    evidence_post_init = evidence_type.__post_init__
    evidence_post_init_code = getattr(evidence_post_init, "__code__", None)
    evidence_authority_payload = evidence_type.authority_payload
    evidence_authority_payload_code = getattr(
        evidence_authority_payload,
        "__code__",
        None,
    )
    evidence_to_payload = evidence_type.to_payload
    evidence_to_payload_code = getattr(evidence_to_payload, "__code__", None)
    evidence_eq = evidence_type.__eq__
    evidence_eq_code = getattr(evidence_eq, "__code__", None)
    evidence_sha_property = evidence_type.__dict__.get("evidence_sha256")
    evidence_sha_getter = getattr(evidence_sha_property, "fget", None)
    evidence_sha_getter_code = getattr(evidence_sha_getter, "__code__", None)
    promotion_property = evidence_type.__dict__.get("promotion_authorized")
    promotion_getter = getattr(promotion_property, "fget", None)
    promotion_getter_code = getattr(promotion_getter, "__code__", None)
    evidence_fields = tuple(
        (name, getattr(evidence_type, name))
        for name in evidence_type.__dataclass_fields__
    )

    store_type = _close._CANONICAL_STORE_TYPE
    store_init = store_type.__init__
    store_init_code = getattr(store_init, "__code__", None)
    if store_init_code is None:
        raise error_type("canonical multiplicity store constructor code is unavailable")

    identity_names = (
        "MultiplicityFamilyCloseError",
        "SequentialDecision",
        "SequentialMultiplicityEvidenceStore",
        "WorkspaceEconomicLock",
        "Path",
        "hashlib",
        "json",
        "_require_canonical_store_read_dispatch",
        "_read_canonical_store_state",
        "_text",
        "_sha256",
        "_positive_int",
        "_canonical_json",
        "_digest",
        "_derive_locked",
        "_canonical_location",
        "_CANONICAL_STORE_TYPE",
        "_CANONICAL_STORE_READ_STATE_DESCRIPTOR",
        "_CANONICAL_STORE_READ_STATE",
        "_CANONICAL_STORE_VALIDATE_ENROLLMENT_DESCRIPTOR",
        "_CANONICAL_STORE_VALIDATE_ENROLLMENT",
    )
    canonical_identities = tuple(
        (name, getattr(_close, name)) for name in identity_names
    )
    canonical_read_code = _close._CANONICAL_STORE_READ_STATE_CODE
    canonical_enrollment_code = _close._CANONICAL_STORE_VALIDATE_ENROLLMENT_CODE
    hashlib_sha256 = _close.hashlib.sha256
    json_dumps = _close.json.dumps

    def require_dispatch() -> None:
        if (
            _close.MultiplicityFamilyCloseError is not error_type
            or _close.TerminalMultiplicityMemberEvidence is not member_type
            or _close.MultiplicityFamilyCloseEvidence is not evidence_type
        ):
            raise error_type("multiplicity family-close public authority changed")

        if (
            store_type.__init__ is not store_init
            or getattr(store_init, "__code__", None) is not store_init_code
        ):
            raise error_type("canonical multiplicity store constructor dispatch changed")

        if (
            member_type.__init__ is not member_init
            or getattr(member_init, "__code__", None) is not member_init_code
            or member_type.__post_init__ is not member_post_init
            or getattr(member_post_init, "__code__", None) is not member_post_init_code
            or member_type.to_payload is not member_to_payload
            or getattr(member_to_payload, "__code__", None) is not member_to_payload_code
            or member_type.__eq__ is not member_eq
            or getattr(member_eq, "__code__", None) is not member_eq_code
            or any(
                getattr(member_type, name, None) is not descriptor
                for name, descriptor in member_fields
            )
        ):
            raise error_type("terminal multiplicity member evidence dispatch changed")

        if (
            evidence_type.__init__ is not evidence_init
            or getattr(evidence_init, "__code__", None) is not evidence_init_code
            or evidence_type.__post_init__ is not evidence_post_init
            or getattr(evidence_post_init, "__code__", None) is not evidence_post_init_code
            or evidence_type.authority_payload is not evidence_authority_payload
            or getattr(evidence_authority_payload, "__code__", None)
            is not evidence_authority_payload_code
            or evidence_type.to_payload is not evidence_to_payload
            or getattr(evidence_to_payload, "__code__", None) is not evidence_to_payload_code
            or evidence_type.__eq__ is not evidence_eq
            or getattr(evidence_eq, "__code__", None) is not evidence_eq_code
            or evidence_type.__dict__.get("evidence_sha256") is not evidence_sha_property
            or getattr(evidence_sha_property, "fget", None) is not evidence_sha_getter
            or getattr(evidence_sha_getter, "__code__", None) is not evidence_sha_getter_code
            or evidence_type.__dict__.get("promotion_authorized") is not promotion_property
            or getattr(promotion_property, "fget", None) is not promotion_getter
            or getattr(promotion_getter, "__code__", None) is not promotion_getter_code
            or any(
                getattr(evidence_type, name, None) is not descriptor
                for name, descriptor in evidence_fields
            )
        ):
            raise error_type("multiplicity family-close evidence dispatch changed")

        for name, expected in canonical_identities:
            if getattr(_close, name, None) is not expected:
                raise error_type(
                    f"multiplicity family-close dependency {name!r} changed"
                )
        if (
            _close._CANONICAL_STORE_READ_STATE_CODE is not canonical_read_code
            or _close._CANONICAL_STORE_VALIDATE_ENROLLMENT_CODE
            is not canonical_enrollment_code
            or _close.hashlib.sha256 is not hashlib_sha256
            or _close.json.dumps is not json_dumps
        ):
            raise error_type("multiplicity family-close primitive dispatch changed")

    def guarded_derive(store):
        if _close.derive_multiplicity_family_close is not guarded_derive:
            raise error_type("multiplicity family-close derive entrypoint was rebound")
        require_dispatch()
        path, workspace_root = _close._canonical_location(store)
        require_dispatch()
        with _close.WorkspaceEconomicLock(workspace_root):
            require_dispatch()
            _close._require_canonical_store_read_dispatch()
            require_dispatch()
            canonical = store_type(
                path,
                workspace_root=workspace_root,
            )
            require_dispatch()
            _close._require_canonical_store_read_dispatch()
            require_dispatch()
            result = _close._derive_locked(canonical)
            require_dispatch()
        if type(result) is not evidence_type:
            raise error_type("family-close derivation returned non-canonical evidence")
        return result

    def guarded_require_current(store, evidence):
        if _close.require_current_multiplicity_family_close is not guarded_require_current:
            raise error_type("multiplicity family-close verifier entrypoint was rebound")
        if type(evidence) is not evidence_type:
            raise TypeError("evidence must be MultiplicityFamilyCloseEvidence")
        require_dispatch()
        path, workspace_root = _close._canonical_location(store)
        require_dispatch()
        with _close.WorkspaceEconomicLock(workspace_root):
            require_dispatch()
            _close._require_canonical_store_read_dispatch()
            require_dispatch()
            canonical = store_type(
                path,
                workspace_root=workspace_root,
            )
            require_dispatch()
            _close._require_canonical_store_read_dispatch()
            require_dispatch()
            current = _close._derive_locked(canonical)
            require_dispatch()
        if current != evidence or current.evidence_sha256 != evidence.evidence_sha256:
            raise error_type(
                "family-close evidence does not match the current canonical multiplicity store"
            )
        return current

    guarded_derive.__name__ = derive_name
    guarded_derive.__qualname__ = derive_qualname
    guarded_derive.__doc__ = derive_doc
    guarded_derive.__module__ = derive_module
    guarded_require_current.__name__ = require_current_name
    guarded_require_current.__qualname__ = require_current_qualname
    guarded_require_current.__doc__ = require_current_doc
    guarded_require_current.__module__ = require_current_module

    _close.derive_multiplicity_family_close = guarded_derive
    _close.require_current_multiplicity_family_close = guarded_require_current


_install_guard()
del _install_guard