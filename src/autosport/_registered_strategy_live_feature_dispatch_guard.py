"""Seal registered-strategy live feature issuance against mutable module dispatch.

The owning feature module already gates ``LiveFeatureObservation`` construction and
pins ScientificRegistry/MarketEvent class dispatch.  Its observer implementation,
however, resolves several authority-bearing helpers and contract constants through
mutable module globals while the issuance ContextVar is active.  This package guard
captures that final public surface and fails closed before a rebound helper can run.

This adds no forecast, probability, intent, execution, or promotion authority.
"""

from __future__ import annotations

from . import registered_strategy_live_feature as _feature


def _install_guard() -> None:
    observer = _feature.observe_registered_strategy_live_features
    observer_code = getattr(observer, "__code__", None)
    error_type = _feature.RegisteredStrategyLiveFeatureError

    observation_type = _feature.LiveFeatureObservation
    observation_init = observation_type.__init__
    observation_init_code = getattr(observation_init, "__code__", None)
    observation_post_init = observation_type.__post_init__
    observation_post_init_code = getattr(observation_post_init, "__code__", None)
    observation_feature_hex = observation_type.__dict__.get("feature_hex")
    observation_field_descriptors = tuple(
        (name, getattr(observation_type, name))
        for name in observation_type.__dataclass_fields__
    )

    authority_type = _feature.RegisteredLiveFeatureAuthority
    authority_init = authority_type.__init__
    authority_init_code = getattr(authority_init, "__code__", None)
    authority_post_init = authority_type.__post_init__
    authority_post_init_code = getattr(authority_post_init, "__code__", None)
    authority_sha_property = authority_type.__dict__.get("authority_sha256")
    authority_sha_getter = getattr(authority_sha_property, "fget", None)
    authority_sha_getter_code = getattr(authority_sha_getter, "__code__", None)
    authority_field_descriptors = tuple(
        (name, getattr(authority_type, name))
        for name in authority_type.__dataclass_fields__
    )

    identity_names = (
        "resolve_registered_live_feature_authority",
        "_canonical_snapshot_events",
        "_canonical_market_event_to_dict",
        "_canonical_market_event_quote_key",
        "_canonical_json_sha256",
        "_canonical_text",
        "_instant",
        "_nonnegative_seconds",
        "MirrorSnapshot",
        "MarketEvent",
        "ScientificRegistry",
        "FeatureSet",
        "ModelVersion",
        "hashlib",
        "json",
        "math",
        "datetime",
        "timezone",
    )
    canonical_identities = tuple(
        (name, getattr(_feature, name)) for name in identity_names
    )

    value_names = (
        "LIVE_FEATURE_SET_ID",
        "LIVE_FEATURE_SET_VERSION",
        "LIVE_FEATURE_FRESHNESS_POLICY_ID",
        "LIVE_FEATURE_DEFINITION_JSON",
        "LIVE_FEATURE_DEFINITION_SHA256",
        "LIVE_FEATURE_SOURCE_CONTRACT_JSON",
        "LIVE_FEATURE_SOURCE_SHA256",
    )
    canonical_values = tuple(
        (name, getattr(_feature, name)) for name in value_names
    )

    hashlib_sha256 = _feature.hashlib.sha256
    json_dumps = _feature.json.dumps
    math_isfinite = _feature.math.isfinite
    datetime_fromisoformat = _feature.datetime.fromisoformat
    timezone_utc = _feature.timezone.utc

    def require_canonical_dispatch() -> None:
        if (
            _feature.RegisteredStrategyLiveFeatureError is not error_type
            or _feature.LiveFeatureObservation is not observation_type
            or _feature.RegisteredLiveFeatureAuthority is not authority_type
            or getattr(observer, "__code__", None) is not observer_code
        ):
            raise error_type("registered live feature public authority changed")

        if (
            observation_type.__init__ is not observation_init
            or getattr(observation_init, "__code__", None) is not observation_init_code
            or observation_type.__post_init__ is not observation_post_init
            or getattr(observation_post_init, "__code__", None)
            is not observation_post_init_code
            or observation_type.__dict__.get("feature_hex") is not observation_feature_hex
            or any(
                getattr(observation_type, name, None) is not descriptor
                for name, descriptor in observation_field_descriptors
            )
        ):
            raise error_type("registered live feature observation type dispatch changed")

        if (
            authority_type.__init__ is not authority_init
            or getattr(authority_init, "__code__", None) is not authority_init_code
            or authority_type.__post_init__ is not authority_post_init
            or getattr(authority_post_init, "__code__", None)
            is not authority_post_init_code
            or authority_type.__dict__.get("authority_sha256") is not authority_sha_property
            or getattr(authority_sha_property, "fget", None) is not authority_sha_getter
            or getattr(authority_sha_getter, "__code__", None)
            is not authority_sha_getter_code
            or any(
                getattr(authority_type, name, None) is not descriptor
                for name, descriptor in authority_field_descriptors
            )
        ):
            raise error_type("registered live feature authority type dispatch changed")

        for name, expected in canonical_identities:
            if getattr(_feature, name, None) is not expected:
                raise error_type(
                    f"registered live feature dependency {name!r} changed"
                )
        for name, expected in canonical_values:
            current = getattr(_feature, name, None)
            if type(current) is not type(expected) or current != expected:
                raise error_type(
                    f"registered live feature contract {name!r} changed"
                )

        if (
            _feature.hashlib.sha256 is not hashlib_sha256
            or _feature.json.dumps is not json_dumps
            or _feature.math.isfinite is not math_isfinite
            or _feature.datetime.fromisoformat is not datetime_fromisoformat
            or _feature.timezone.utc is not timezone_utc
        ):
            raise error_type("registered live feature primitive dispatch changed")

    def guarded_observer(*args, **kwargs):
        if _feature.observe_registered_strategy_live_features is not guarded_observer:
            raise error_type("registered live feature public observer was rebound")
        require_canonical_dispatch()
        result = observer(*args, **kwargs)
        require_canonical_dispatch()
        if type(result) is not tuple or any(
            type(item) is not observation_type for item in result
        ):
            raise error_type(
                "registered live feature observer returned non-canonical evidence"
            )
        return result

    guarded_observer.__name__ = observer.__name__
    guarded_observer.__qualname__ = observer.__qualname__
    guarded_observer.__doc__ = observer.__doc__
    guarded_observer.__module__ = observer.__module__
    _feature.observe_registered_strategy_live_features = guarded_observer


_install_guard()
del _install_guard
