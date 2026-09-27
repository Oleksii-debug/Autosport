from __future__ import annotations

"""Install product-owned curation before provider-sport mappings become positive truth.

Exact caller-authored JSON bytes prove integrity only. They do not prove that a
provider sport identity maps to an Autosport canonical sport. The generic durable
registry therefore remains the storage/chronology authority, while this thin guard
supplies the missing product curation authority for mappings that Autosport has
explicitly admitted.

The table is intentionally small and exact. Adding another provider sport is a
source-reviewed product decision, not a runtime/caller capability. The wrapped
registry still owns parsing, chronology, locking and persistence; this module creates
no second store or provider transport.
"""

from types import MappingProxyType

from . import provider_sport_mapping as mapping


# Betfair eventTypeId 1/2 are the product's currently admitted canonical mappings.
# Keep provider identifiers opaque; do not infer new mappings from textual shape.
_PRODUCT_CURATED_PROVIDER_SPORTS = MappingProxyType(
    {
        ("betfair", "1"): "football",
        ("betfair", "2"): "tennis",
    }
)


def _install_guard() -> None:
    # Long-lived guard closures must not resolve authority-bearing helpers through this
    # module's mutable globals after installation. Capture both the owning module and
    # interpreter helpers once, before positive mapping APIs become available.
    mapping_module = mapping
    exact_vars = vars
    exact_getattr = getattr
    exact_type = type
    exact_callable = callable
    exact_runtime_error = RuntimeError

    registry_type = mapping_module.ProviderSportMappingRegistry
    current_register = registry_type.register_evidence
    current_resolve = registry_type.resolve
    if exact_getattr(current_register, "_product_curation_authority_guard", False):
        if not exact_getattr(current_resolve, "_product_curation_authority_guard", False):
            raise exact_runtime_error("provider sport curation guard is only partially installed")
        return

    evidence_descriptor = exact_vars(mapping_module.ProviderSportEvidence).get("from_exact_bytes")
    if exact_type(evidence_descriptor) is not classmethod:
        raise exact_runtime_error("ProviderSportEvidence.from_exact_bytes must remain a classmethod")
    canonical_parser = evidence_descriptor.__func__
    canonical_parser_code = canonical_parser.__code__
    canonical_parser_globals = canonical_parser.__globals__
    canonical_registry_register = current_register
    canonical_registry_register_code = current_register.__code__
    canonical_registry_resolve = current_resolve
    canonical_registry_resolve_code = current_resolve.__code__
    canonical_evidence_type = mapping_module.ProviderSportEvidence
    canonical_binding_type = mapping_module.ProviderSportBinding
    canonical_resolution_type = mapping_module.CanonicalSportResolution
    canonical_error = mapping_module.ProviderSportMappingError
    curated = _PRODUCT_CURATED_PROVIDER_SPORTS

    # The canonical parser intentionally late-resolves helpers from its owning module.
    # Product curation must therefore witness that *real* namespace and the complete
    # direct parser graph, rather than trusting a replaceable module-level ``globals``
    # observer. This closes self-restoring helper/stale-view attacks between the
    # curation parse and the canonical registry's parse of the same immutable bytes.
    parser_helper_names = (
        "_strict_json_object",
        "_canonical_text",
        "_opaque_provider_id",
        "_canonical_sport",
        "_time_text",
        "_instant",
    )
    canonical_parser_helpers = tuple(
        (name, exact_getattr(mapping_module, name)) for name in parser_helper_names
    )
    canonical_parser_helper_codes = tuple(
        (name, helper, exact_getattr(helper, "__code__", None))
        for name, helper in canonical_parser_helpers
    )
    canonical_hashlib = mapping_module.hashlib
    canonical_json = mapping_module.json
    canonical_json_loads = canonical_json.loads
    canonical_json_loads_code = exact_getattr(canonical_json_loads, "__code__", None)
    canonical_json_decoder = canonical_json.JSONDecoder
    canonical_json_decoder_init = exact_vars(canonical_json_decoder).get("__init__")
    canonical_json_decoder_decode = exact_vars(canonical_json_decoder).get("decode")
    canonical_json_decoder_raw_decode = exact_vars(canonical_json_decoder).get("raw_decode")
    canonical_json_decoder_codes = tuple(
        (member, exact_getattr(member, "__code__", None))
        for member in (
            canonical_json_decoder_init,
            canonical_json_decoder_decode,
            canonical_json_decoder_raw_decode,
        )
    )
    canonical_unicodedata = mapping_module.unicodedata
    canonical_datetime = mapping_module.datetime
    canonical_timezone = mapping_module.timezone
    canonical_evidence_schema = mapping_module._EVIDENCE_SCHEMA
    canonical_evidence_version = mapping_module._EVIDENCE_VERSION
    canonical_reserved_sports = mapping_module._RESERVED_SPORTS

    canonical_payload = exact_vars(registry_type).get("_payload")
    if not exact_callable(canonical_payload):
        raise exact_runtime_error("ProviderSportMappingRegistry._payload must remain callable")
    canonical_payload_code = canonical_payload.__code__
    unsigned_descriptor = exact_vars(registry_type).get("_unsigned_payload")
    if exact_type(unsigned_descriptor) is not staticmethod:
        raise exact_runtime_error("ProviderSportMappingRegistry._unsigned_payload must remain static")
    canonical_unsigned_payload = unsigned_descriptor.__func__
    canonical_unsigned_payload_code = canonical_unsigned_payload.__code__

    canonical_binding_semantic_payload = exact_vars(canonical_binding_type).get("semantic_payload")
    canonical_binding_payload = exact_vars(canonical_binding_type).get("payload")
    canonical_binding_id_descriptor = exact_vars(canonical_binding_type).get("binding_id")
    if not exact_callable(canonical_binding_semantic_payload) or not exact_callable(canonical_binding_payload):
        raise exact_runtime_error("ProviderSportBinding payload serializers must remain callable")
    if exact_type(canonical_binding_id_descriptor) is not property or canonical_binding_id_descriptor.fget is None:
        raise exact_runtime_error("ProviderSportBinding.binding_id must remain a property")
    canonical_binding_semantic_payload_code = canonical_binding_semantic_payload.__code__
    canonical_binding_payload_code = canonical_binding_payload.__code__
    canonical_binding_id_getter = canonical_binding_id_descriptor.fget
    canonical_binding_id_code = canonical_binding_id_getter.__code__

    canonical_digest = mapping_module._digest
    canonical_digest_code = canonical_digest.__code__
    canonical_atomic_write_json = mapping_module.atomic_write_json
    canonical_atomic_write_json_code = exact_getattr(canonical_atomic_write_json, "__code__", None)
    canonical_durable_path_lock = mapping_module.durable_path_lock
    canonical_durable_path_lock_code = exact_getattr(canonical_durable_path_lock, "__code__", None)
    canonical_schema = mapping_module._SCHEMA
    canonical_version = mapping_module._VERSION

    def _require_curated(
        provider_namespace: str,
        provider_sport_id: str,
        canonical_sport: str,
    ) -> None:
        expected = curated.get((provider_namespace, provider_sport_id))
        if expected is None or canonical_sport != expected:
            raise canonical_error(
                "provider sport mapping lacks product-owned curation authority"
            )

    def _assert_parser_authority() -> None:
        if canonical_parser.__globals__ is not canonical_parser_globals:
            raise canonical_error("canonical evidence parser namespace authority changed")
        # A module-global name called ``globals`` shadows the builtin used by the
        # generated canonical register method. Reject it directly from the captured
        # real namespace so a forged stale view cannot attest a hostile helper graph.
        if "globals" in canonical_parser_globals:
            raise canonical_error("canonical evidence parser namespace observer changed")
        for name, helper, expected_code in canonical_parser_helper_codes:
            if canonical_parser_globals.get(name) is not helper:
                raise canonical_error("canonical evidence parser helper authority changed")
            if expected_code is not None and exact_getattr(helper, "__code__", None) is not expected_code:
                raise canonical_error("canonical evidence parser helper executable authority changed")
        if (
            canonical_parser_globals.get("hashlib") is not canonical_hashlib
            or canonical_parser_globals.get("json") is not canonical_json
            or canonical_parser_globals.get("unicodedata") is not canonical_unicodedata
            or canonical_parser_globals.get("datetime") is not canonical_datetime
            or canonical_parser_globals.get("timezone") is not canonical_timezone
            or canonical_parser_globals.get("ProviderSportMappingError") is not canonical_error
            or canonical_parser_globals.get("ProviderSportEvidence") is not canonical_evidence_type
            or canonical_parser_globals.get("_EVIDENCE_SCHEMA") != canonical_evidence_schema
            or canonical_parser_globals.get("_EVIDENCE_VERSION") != canonical_evidence_version
            or canonical_parser_globals.get("_RESERVED_SPORTS") is not canonical_reserved_sports
        ):
            raise canonical_error("canonical evidence parser dependency authority changed")
        # ``_strict_json_object`` calls into the mutable stdlib ``json`` module. Merely
        # witnessing the module object is insufficient: callers can replace
        # ``json.loads`` in-place, self-restore during the first curation parse, and let
        # the registry's later parse observe different bytes. Pin the exact decoder
        # entry and its direct executable graph before any parser code is allowed to run.
        if (
            canonical_json.loads is not canonical_json_loads
            or exact_getattr(canonical_json_loads, "__code__", None) is not canonical_json_loads_code
            or canonical_json.JSONDecoder is not canonical_json_decoder
            or exact_vars(canonical_json_decoder).get("__init__") is not canonical_json_decoder_init
            or exact_vars(canonical_json_decoder).get("decode") is not canonical_json_decoder_decode
            or exact_vars(canonical_json_decoder).get("raw_decode") is not canonical_json_decoder_raw_decode
        ):
            raise canonical_error("canonical evidence parser transitive JSON authority changed")
        for member, expected_code in canonical_json_decoder_codes:
            if member is not None and expected_code is not None and exact_getattr(member, "__code__", None) is not expected_code:
                raise canonical_error("canonical evidence parser transitive JSON executable authority changed")

    def _assert_guard_authority(registry=None) -> None:
        if (
            registry_type.register_evidence is not register_evidence
            or registry_type.resolve is not resolve
        ):
            raise canonical_error("provider sport curation authority changed")
        descriptor = exact_vars(canonical_evidence_type).get("from_exact_bytes")
        if (
            descriptor is not evidence_descriptor
            or canonical_parser.__code__ is not canonical_parser_code
            or canonical_registry_register.__code__ is not canonical_registry_register_code
            or canonical_registry_resolve.__code__ is not canonical_registry_resolve_code
        ):
            raise canonical_error("provider sport curation authority changed")
        _assert_parser_authority()

        if (
            exact_vars(registry_type).get("_payload") is not canonical_payload
            or canonical_payload.__code__ is not canonical_payload_code
            or exact_vars(registry_type).get("_unsigned_payload") is not unsigned_descriptor
            or canonical_unsigned_payload.__code__ is not canonical_unsigned_payload_code
        ):
            raise canonical_error("provider sport durable writer dispatch authority changed")
        if registry is not None and (
            "_payload" in exact_vars(registry) or "_unsigned_payload" in exact_vars(registry)
        ):
            raise canonical_error("provider sport durable writer instance authority changed")

        if (
            exact_vars(canonical_binding_type).get("semantic_payload") is not canonical_binding_semantic_payload
            or canonical_binding_semantic_payload.__code__ is not canonical_binding_semantic_payload_code
            or exact_vars(canonical_binding_type).get("payload") is not canonical_binding_payload
            or canonical_binding_payload.__code__ is not canonical_binding_payload_code
            or exact_vars(canonical_binding_type).get("binding_id") is not canonical_binding_id_descriptor
            or canonical_binding_id_getter.__code__ is not canonical_binding_id_code
        ):
            raise canonical_error("provider sport durable binding serialization authority changed")

        if (
            mapping_module._digest is not canonical_digest
            or canonical_digest.__code__ is not canonical_digest_code
            or mapping_module.atomic_write_json is not canonical_atomic_write_json
            or exact_getattr(canonical_atomic_write_json, "__code__", None)
            is not canonical_atomic_write_json_code
            or mapping_module.durable_path_lock is not canonical_durable_path_lock
            or exact_getattr(canonical_durable_path_lock, "__code__", None)
            is not canonical_durable_path_lock_code
            or mapping_module._SCHEMA != canonical_schema
            or mapping_module._VERSION != canonical_version
        ):
            raise canonical_error("provider sport durable writer dependency authority changed")

    def register_evidence(self, source_snapshot_bytes: bytes):
        # Fail before durable mutation if any authority-bearing surface moved.
        _assert_guard_authority(self)
        evidence = canonical_parser(canonical_evidence_type, source_snapshot_bytes)
        # A parser helper is allowed to execute no authority-changing callback. Recheck
        # the exact transitive graph immediately after the one product-curation parse so
        # a helper cannot mutate/restore a sibling dependency around this decision.
        _assert_guard_authority(self)
        _require_curated(
            evidence.provider_namespace,
            evidence.provider_sport_id,
            evidence.canonical_sport,
        )

        # Re-check immediately before the irreversible canonical mutation. Reuse the
        # canonical registry for every durability and chronology invariant.
        _assert_guard_authority(self)
        binding = canonical_registry_register(self, source_snapshot_bytes)
        _assert_guard_authority(self)
        if exact_type(binding) is not canonical_binding_type:
            raise canonical_error("provider sport registration authority changed")
        if (
            binding.provider_namespace != evidence.provider_namespace
            or binding.provider_sport_id != evidence.provider_sport_id
            or binding.canonical_sport != evidence.canonical_sport
            or binding.valid_from != evidence.valid_from
            or binding.valid_until != evidence.valid_until
            or binding.evidence_available_at != evidence.evidence_available_at
            or binding.source_snapshot_sha256 != evidence.source_snapshot_sha256
        ):
            raise canonical_error("provider sport registration semantic interpretation changed")
        return binding

    def resolve(self, *, provider_namespace: str, provider_sport_id: str, as_of: str):
        # A registry created by a pre-curation version can survive restart. Durable
        # shape/digest truth is not sufficient to grandfather that old assertion into
        # positive product truth, so re-apply curation at every positive resolution.
        _assert_guard_authority(self)
        resolution = canonical_registry_resolve(
            self,
            provider_namespace=provider_namespace,
            provider_sport_id=provider_sport_id,
            as_of=as_of,
        )
        _assert_guard_authority(self)
        if exact_type(resolution) is not canonical_resolution_type:
            raise canonical_error("provider sport resolution authority changed")
        _require_curated(
            resolution.provider_namespace,
            resolution.provider_sport_id,
            resolution.canonical_sport,
        )
        return resolution

    setattr(register_evidence, "_product_curation_authority_guard", True)
    setattr(resolve, "_product_curation_authority_guard", True)
    registry_type.register_evidence = register_evidence
    registry_type.resolve = resolve


_install_guard()
del _install_guard

__all__: list[str] = []