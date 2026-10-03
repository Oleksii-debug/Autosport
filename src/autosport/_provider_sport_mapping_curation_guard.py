from __future__ import annotations

"""Install product-owned curation before provider-sport mappings become positive truth.

Caller-authored JSON bytes are structural evidence only. They cannot mint a positive
provider-to-canonical sport mapping. Positive publication is limited to source-reviewed
records frozen in product code; callers may select only an admitted provider identity,
not the canonical sport, validity interval, evidence time, or source digest.

The guard deliberately parses each frozen product record exactly once and carries that
exact ``ProviderSportEvidence`` object through the canonical registry's captured
load/overlap/persist primitives. This preserves the existing parser, chronology,
durability and resolution authorities without creating a second mapping stack.
"""

from types import MappingProxyType

from . import provider_sport_mapping as mapping


# These are product-curation assertions, not provider-origin claims. The validity
# boundary is the exact durable commit instant that first admitted this fixed product
# authority; this code does not claim product authority before that point.
_PRODUCT_CURATED_PROVIDER_SPORTS = MappingProxyType(
    {
        ("betfair", "1"): (
            "football",
            "2026-09-27T12:21:13Z",
            None,
            "2026-09-27T12:21:13Z",
            b'{"canonical_sport":"football","evidence_available_at":"2026-09-27T12:21:13Z",'
            b'"provider_namespace":"betfair","provider_sport_id":"1",'
            b'"schema":"autosport.provider_sport_mapping_evidence","schema_version":1,'
            b'"valid_from":"2026-09-27T12:21:13Z","valid_until":null}',
            "ac6c986666014a7b0f9da74384216baae421d9a07556454d5cd384eaf8c5a3f7",
            "f54566089d0ec407b4f1fc9c09286bd85147aab37d4e865baf4aa96fa5c5635c",
        ),
        ("betfair", "2"): (
            "tennis",
            "2026-09-27T12:21:13Z",
            None,
            "2026-09-27T12:21:13Z",
            b'{"canonical_sport":"tennis","evidence_available_at":"2026-09-27T12:21:13Z",'
            b'"provider_namespace":"betfair","provider_sport_id":"2",'
            b'"schema":"autosport.provider_sport_mapping_evidence","schema_version":1,'
            b'"valid_from":"2026-09-27T12:21:13Z","valid_until":null}',
            "57bf4b1ca3decf42fcc361d511db401db1ef464397028b5aa26baac34c76bef4",
            "c6c3794645619337fe0d616b7ac3f9e14ca126d7eac47e6bcd591a9085753a58",
        ),
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
    exact_any = any
    exact_len = len
    exact_sorted = sorted

    registry_type = mapping_module.ProviderSportMappingRegistry
    current_register = registry_type.register_evidence
    current_resolve = registry_type.resolve
    existing_curated = exact_getattr(registry_type, "register_curated", None)
    if exact_getattr(current_register, "_product_curation_authority_guard", False):
        if (
            not exact_getattr(current_resolve, "_product_curation_authority_guard", False)
            or not exact_getattr(existing_curated, "_product_curation_authority_guard", False)
        ):
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

    evidence_field_names = (
        "provider_namespace",
        "provider_sport_id",
        "canonical_sport",
        "valid_from",
        "valid_until",
        "evidence_available_at",
        "source_snapshot_sha256",
    )
    binding_field_names = (
        *evidence_field_names,
        "recorded_at",
    )
    resolution_field_names = (
        "provider_namespace",
        "provider_sport_id",
        "canonical_sport",
        "as_of",
        "binding_id",
        "source_snapshot_sha256",
        "registry_sha256",
    )

    def _capture_surface(cls, field_names):
        initializer = exact_vars(cls).get("__init__")
        if not exact_callable(initializer):
            raise exact_runtime_error(f"{cls.__name__} initializer must remain callable")
        return (
            cls.__getattribute__,
            initializer,
            exact_getattr(initializer, "__code__", None),
            tuple((name, exact_vars(cls).get(name)) for name in field_names),
        )

    evidence_surface = _capture_surface(canonical_evidence_type, evidence_field_names)
    binding_surface = _capture_surface(canonical_binding_type, binding_field_names)
    resolution_surface = _capture_surface(canonical_resolution_type, resolution_field_names)

    # The canonical parser intentionally late-resolves helpers from its owning module.
    # Product curation must therefore witness that real namespace and the complete
    # direct parser graph rather than trust a replaceable module-level observer.
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
    canonical_sha256 = canonical_hashlib.sha256
    canonical_sha256_code = exact_getattr(canonical_sha256, "__code__", None)
    canonical_json = mapping_module.json
    canonical_json_loads = canonical_json.loads
    canonical_json_loads_code = exact_getattr(canonical_json_loads, "__code__", None)
    canonical_json_dumps = canonical_json.dumps
    canonical_json_dumps_code = exact_getattr(canonical_json_dumps, "__code__", None)
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

    # Capture the exact mutation primitives once. The wrapper consumes one parsed
    # evidence object but does not invent a second durability/overlap authority.
    canonical_clock = mapping_module._utc_now
    canonical_clock_code = canonical_clock.__code__
    canonical_instant = mapping_module._instant
    canonical_instant_code = canonical_instant.__code__
    canonical_load = exact_vars(registry_type).get("_load")
    canonical_persist = exact_vars(registry_type).get("_persist")
    overlap_descriptor = exact_vars(registry_type).get("_assert_no_overlap")
    if not exact_callable(canonical_load) or not exact_callable(canonical_persist):
        raise exact_runtime_error("provider sport registry load/persist must remain callable")
    if exact_type(overlap_descriptor) is not staticmethod:
        raise exact_runtime_error("ProviderSportMappingRegistry._assert_no_overlap must remain static")
    canonical_load_code = canonical_load.__code__
    canonical_persist_code = canonical_persist.__code__
    canonical_overlap = overlap_descriptor.__func__
    canonical_overlap_code = canonical_overlap.__code__

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
    canonical_atomic_write_globals = exact_getattr(canonical_atomic_write_json, "__globals__", None)
    if exact_type(canonical_atomic_write_globals) is not dict:
        raise exact_runtime_error("provider sport atomic writer namespace is unavailable")
    canonical_atomic_os = canonical_atomic_write_globals.get("os")
    canonical_atomic_json = canonical_atomic_write_globals.get("json")
    canonical_atomic_tempfile = canonical_atomic_write_globals.get("tempfile")
    canonical_atomic_os_replace = exact_getattr(canonical_atomic_os, "replace", None)
    canonical_atomic_os_fsync = exact_getattr(canonical_atomic_os, "fsync", None)
    canonical_atomic_json_dump = exact_getattr(canonical_atomic_json, "dump", None)
    canonical_atomic_named_temporary_file = exact_getattr(
        canonical_atomic_tempfile,
        "NamedTemporaryFile",
        None,
    )
    if not all(
        exact_callable(value)
        for value in (
            canonical_atomic_os_replace,
            canonical_atomic_os_fsync,
            canonical_atomic_json_dump,
            canonical_atomic_named_temporary_file,
        )
    ):
        raise exact_runtime_error("provider sport atomic writer dependencies are unavailable")
    canonical_durable_path_lock = mapping_module.durable_path_lock
    canonical_durable_path_lock_code = exact_getattr(canonical_durable_path_lock, "__code__", None)
    canonical_schema = mapping_module._SCHEMA
    canonical_version = mapping_module._VERSION

    canonical_path_class = mapping_module.Path
    canonical_concrete_path_type = exact_type(canonical_path_class("."))
    canonical_concrete_path_mro = canonical_concrete_path_type.__mro__

    def _resolve_concrete_path_callable(name: str):
        for owner in canonical_concrete_path_mro:
            slot = exact_vars(owner).get(name)
            if slot is None:
                continue
            if not exact_callable(slot):
                raise exact_runtime_error(
                    f"provider sport durable path callable is unavailable: {name}"
                )
            return owner, slot, exact_getattr(slot, "__code__", None)
        raise exact_runtime_error(
            f"provider sport durable path callable is unavailable: {name}"
        )

    canonical_concrete_path_surface = tuple(
        (name, *_resolve_concrete_path_callable(name))
        for name in ("exists", "read_text", "open")
    )

    canonical_path_exists = canonical_path_class.exists
    canonical_path_exists_code = exact_getattr(canonical_path_exists, "__code__", None)
    canonical_path_read_text = canonical_path_class.read_text
    canonical_path_read_text_code = exact_getattr(canonical_path_read_text, "__code__", None)
    canonical_path_open = canonical_path_class.open
    canonical_path_open_code = exact_getattr(canonical_path_open, "__code__", None)
    canonical_path_open_globals = exact_getattr(canonical_path_open, "__globals__", None)
    canonical_path_io = (
        canonical_path_open_globals.get("io")
        if exact_type(canonical_path_open_globals) is dict
        else None
    )
    canonical_io_open = (
        exact_getattr(canonical_path_io, "open", None)
        if canonical_path_io is not None
        else None
    )

    def _assert_surface(cls, expected_surface, label: str) -> None:
        expected_getattribute, expected_init, expected_init_code, descriptors = expected_surface
        current_init = exact_vars(cls).get("__init__")
        if (
            cls.__getattribute__ is not expected_getattribute
            or current_init is not expected_init
            or (
                expected_init_code is not None
                and exact_getattr(expected_init, "__code__", None) is not expected_init_code
            )
        ):
            raise canonical_error(f"provider sport {label} field surface authority changed")
        for name, descriptor in descriptors:
            if exact_vars(cls).get(name) is not descriptor:
                raise canonical_error(f"provider sport {label} field surface authority changed")

    def _curated_record(provider_namespace: str, provider_sport_id: str):
        # Do not invoke caller-defined hash/equality code while selecting product
        # authority. Only exact built-in strings may address the frozen table.
        if exact_type(provider_namespace) is not str or exact_type(provider_sport_id) is not str:
            raise canonical_error("provider sport mapping lacks product-owned curation authority")
        record = curated.get((provider_namespace, provider_sport_id))
        if record is None:
            raise canonical_error("provider sport mapping lacks product-owned curation authority")
        return record

    def _require_curated_evidence(
        evidence,
        *,
        provider_namespace: str,
        provider_sport_id: str,
        record,
    ) -> None:
        (
            expected_sport,
            expected_valid_from,
            expected_valid_until,
            expected_available,
            _raw,
            expected_source_sha256,
            _expected_binding_id,
        ) = record
        if (
            exact_type(evidence) is not canonical_evidence_type
            or evidence.provider_namespace != provider_namespace
            or evidence.provider_sport_id != provider_sport_id
            or evidence.canonical_sport != expected_sport
            or evidence.valid_from != expected_valid_from
            or evidence.valid_until != expected_valid_until
            or evidence.evidence_available_at != expected_available
            or evidence.source_snapshot_sha256 != expected_source_sha256
        ):
            raise canonical_error("provider sport mapping lacks product-owned curation provenance")

    def _require_curated_binding(binding, record) -> None:
        (
            expected_sport,
            expected_valid_from,
            expected_valid_until,
            expected_available,
            _raw,
            expected_source_sha256,
            expected_binding_id,
        ) = record
        if (
            exact_type(binding) is not canonical_binding_type
            or binding.canonical_sport != expected_sport
            or binding.valid_from != expected_valid_from
            or binding.valid_until != expected_valid_until
            or binding.evidence_available_at != expected_available
            or binding.source_snapshot_sha256 != expected_source_sha256
            or binding.binding_id != expected_binding_id
        ):
            raise canonical_error("provider sport mapping lacks product-owned curation provenance")

    def _require_curated_resolution(registry, resolution) -> None:
        record = _curated_record(
            resolution.provider_namespace,
            resolution.provider_sport_id,
        )
        (
            expected_sport,
            _expected_valid_from,
            _expected_valid_until,
            _expected_available,
            _raw,
            expected_source_sha256,
            expected_binding_id,
        ) = record
        if (
            exact_type(resolution) is not canonical_resolution_type
            or resolution.canonical_sport != expected_sport
            or resolution.source_snapshot_sha256 != expected_source_sha256
            or resolution.binding_id != expected_binding_id
            or exact_type(registry._bindings) is not list
        ):
            raise canonical_error("provider sport mapping lacks product-owned curation provenance")
        matches = [
            binding for binding in registry._bindings
            if binding.binding_id == expected_binding_id
        ]
        if exact_len(matches) != 1:
            raise canonical_error("provider sport mapping lacks product-owned curation provenance")
        _require_curated_binding(matches[0], record)

    def _assert_parser_authority() -> None:
        if canonical_parser.__globals__ is not canonical_parser_globals:
            raise canonical_error("canonical evidence parser namespace authority changed")
        if "globals" in canonical_parser_globals:
            raise canonical_error("canonical evidence parser namespace observer changed")
        for name, helper, expected_code in canonical_parser_helper_codes:
            if canonical_parser_globals.get(name) is not helper:
                raise canonical_error("canonical evidence parser helper authority changed")
            if expected_code is not None and exact_getattr(helper, "__code__", None) is not expected_code:
                raise canonical_error("canonical evidence parser helper executable authority changed")
        if (
            canonical_parser_globals.get("hashlib") is not canonical_hashlib
            or canonical_hashlib.sha256 is not canonical_sha256
            or (
                canonical_sha256_code is not None
                and exact_getattr(canonical_sha256, "__code__", None) is not canonical_sha256_code
            )
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
        if (
            canonical_json.loads is not canonical_json_loads
            or exact_getattr(canonical_json_loads, "__code__", None) is not canonical_json_loads_code
            or canonical_json.dumps is not canonical_json_dumps
            or exact_getattr(canonical_json_dumps, "__code__", None) is not canonical_json_dumps_code
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
        if registry is not None and exact_type(registry) is not registry_type:
            raise canonical_error("canonical provider sport registry type is required")
        if (
            mapping_module.ProviderSportMappingRegistry is not registry_type
            or registry_type.register_evidence is not register_evidence
            or registry_type.register_curated is not register_curated
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
        _assert_surface(canonical_evidence_type, evidence_surface, "evidence")
        _assert_surface(canonical_binding_type, binding_surface, "binding")
        _assert_surface(canonical_resolution_type, resolution_surface, "resolution")
        _assert_parser_authority()

        if (
            mapping_module._utc_now is not canonical_clock
            or canonical_clock.__code__ is not canonical_clock_code
            or mapping_module._instant is not canonical_instant
            or canonical_instant.__code__ is not canonical_instant_code
            or exact_vars(registry_type).get("_load") is not canonical_load
            or canonical_load.__code__ is not canonical_load_code
            or exact_vars(registry_type).get("_persist") is not canonical_persist
            or canonical_persist.__code__ is not canonical_persist_code
            or exact_vars(registry_type).get("_assert_no_overlap") is not overlap_descriptor
            or canonical_overlap.__code__ is not canonical_overlap_code
        ):
            raise canonical_error("provider sport canonical mutation authority changed")
        if registry is not None and exact_any(
            name in exact_vars(registry)
            for name in ("_load", "_persist", "_assert_no_overlap")
        ):
            raise canonical_error("provider sport canonical mutation instance authority changed")

        for name, expected_owner, expected_slot, expected_code in canonical_concrete_path_surface:
            current_owner, current_slot, _current_code = _resolve_concrete_path_callable(name)
            if current_owner is not expected_owner or current_slot is not expected_slot:
                raise canonical_error(
                    "provider sport durable concrete path dispatch authority changed"
                )
            if (
                expected_code is not None
                and exact_getattr(expected_slot, "__code__", None) is not expected_code
            ):
                raise canonical_error(
                    "provider sport durable concrete path executable authority changed"
                )

        if (
            mapping_module.Path is not canonical_path_class
            or canonical_path_class.exists is not canonical_path_exists
            or (
                canonical_path_exists_code is not None
                and exact_getattr(canonical_path_exists, "__code__", None)
                is not canonical_path_exists_code
            )
            or canonical_path_class.read_text is not canonical_path_read_text
            or (
                canonical_path_read_text_code is not None
                and exact_getattr(canonical_path_read_text, "__code__", None)
                is not canonical_path_read_text_code
            )
            or canonical_path_class.open is not canonical_path_open
            or (
                canonical_path_open_code is not None
                and exact_getattr(canonical_path_open, "__code__", None)
                is not canonical_path_open_code
            )
            or exact_getattr(canonical_path_open, "__globals__", None)
            is not canonical_path_open_globals
            or (
                canonical_path_io is not None
                and exact_getattr(canonical_path_io, "open", None) is not canonical_io_open
            )
        ):
            raise canonical_error("provider sport durable path read authority changed")
        if registry is not None and exact_type(registry.path) is not canonical_concrete_path_type:
            raise canonical_error("provider sport durable path instance authority changed")

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
            or mapping_module.ProviderSportBinding is not canonical_binding_type
        ):
            raise canonical_error("provider sport durable binding serialization authority changed")

        if (
            mapping_module._digest is not canonical_digest
            or canonical_digest.__code__ is not canonical_digest_code
            or mapping_module.atomic_write_json is not canonical_atomic_write_json
            or exact_getattr(canonical_atomic_write_json, "__code__", None)
            is not canonical_atomic_write_json_code
            or exact_getattr(canonical_atomic_write_json, "__globals__", None)
            is not canonical_atomic_write_globals
            or canonical_atomic_write_globals.get("os") is not canonical_atomic_os
            or canonical_atomic_write_globals.get("json") is not canonical_atomic_json
            or canonical_atomic_write_globals.get("tempfile") is not canonical_atomic_tempfile
            or exact_getattr(canonical_atomic_os, "replace", None) is not canonical_atomic_os_replace
            or exact_getattr(canonical_atomic_os, "fsync", None) is not canonical_atomic_os_fsync
            or exact_getattr(canonical_atomic_json, "dump", None) is not canonical_atomic_json_dump
            or exact_getattr(canonical_atomic_tempfile, "NamedTemporaryFile", None)
            is not canonical_atomic_named_temporary_file
            or mapping_module.durable_path_lock is not canonical_durable_path_lock
            or exact_getattr(canonical_durable_path_lock, "__code__", None)
            is not canonical_durable_path_lock_code
            or mapping_module._SCHEMA != canonical_schema
            or mapping_module._VERSION != canonical_version
        ):
            raise canonical_error("provider sport durable writer dependency authority changed")

    def register_evidence(self, source_snapshot_bytes: bytes):
        _assert_guard_authority(self)
        raise canonical_error(
            "caller-authored exact bytes are not positive provider sport mapping authority; "
            "use register_curated(provider_namespace=..., provider_sport_id=...)"
        )

    def register_curated(
        self,
        *,
        provider_namespace: str,
        provider_sport_id: str,
    ):
        _assert_guard_authority(self)
        record = _curated_record(provider_namespace, provider_sport_id)
        (
            _expected_sport,
            _expected_valid_from,
            _expected_valid_until,
            _expected_available,
            source_snapshot_bytes,
            _expected_source_sha256,
            expected_binding_id,
        ) = record

        # One and only one semantic parse occurs on the positive product-curation path.
        evidence = canonical_parser(canonical_evidence_type, source_snapshot_bytes)
        _assert_guard_authority(self)
        _require_curated_evidence(
            evidence,
            provider_namespace=provider_namespace,
            provider_sport_id=provider_sport_id,
            record=record,
        )

        # Carry that exact parsed object through the canonical registry mutation
        # primitives. The public raw-byte API is never delegated to for positive truth.
        with canonical_durable_path_lock(self.path):
            _assert_guard_authority(self)
            if not self.path.exists():
                raise canonical_error(
                    "durable provider sport mapping registry is missing; initialize it first"
                )
            canonical_load(self)
            _assert_guard_authority(self)

            recorded_at = canonical_clock()
            _assert_guard_authority(self)
            if canonical_instant("recorded_at", recorded_at) < canonical_instant(
                "evidence_available_at", evidence.evidence_available_at
            ):
                raise canonical_error(
                    "mapping evidence availability cannot be in the future of product recording time"
                )

            candidate = canonical_binding_type(
                provider_namespace=evidence.provider_namespace,
                provider_sport_id=evidence.provider_sport_id,
                canonical_sport=evidence.canonical_sport,
                valid_from=evidence.valid_from,
                valid_until=evidence.valid_until,
                evidence_available_at=evidence.evidence_available_at,
                source_snapshot_sha256=evidence.source_snapshot_sha256,
                recorded_at=recorded_at,
            )
            _assert_guard_authority(self)
            _require_curated_binding(candidate, record)
            if candidate.binding_id != expected_binding_id:
                raise canonical_error("product-curated provider sport binding identity changed")
            for existing in self._bindings:
                if existing.binding_id == candidate.binding_id:
                    _require_curated_binding(existing, record)
                    return existing

            canonical_overlap(candidate, self._bindings)
            updated = exact_sorted(
                [*self._bindings, candidate],
                key=lambda value: (
                    value.provider_namespace,
                    value.provider_sport_id,
                    value.valid_from,
                    value.valid_until is None,
                    value.valid_until or "",
                    value.binding_id,
                ),
            )
            _assert_guard_authority(self)
            canonical_persist(self, updated)
            self._bindings = updated
            _assert_guard_authority(self)
            _require_curated_binding(candidate, record)
            return candidate

    def resolve(self, *, provider_namespace: str, provider_sport_id: str, as_of: str):
        # Durable shape/digest truth alone does not grandfather a caller-written
        # matching tuple. Positive resolution must bind the exact frozen product
        # curation provenance as well as the provider/sport identity.
        _assert_guard_authority(self)
        resolution = canonical_registry_resolve(
            self,
            provider_namespace=provider_namespace,
            provider_sport_id=provider_sport_id,
            as_of=as_of,
        )
        _assert_guard_authority(self)
        _require_curated_resolution(self, resolution)
        return resolution

    setattr(register_evidence, "_product_curation_authority_guard", True)
    setattr(register_curated, "_product_curation_authority_guard", True)
    setattr(resolve, "_product_curation_authority_guard", True)
    registry_type.register_evidence = register_evidence
    registry_type.register_curated = register_curated
    registry_type.resolve = resolve


_install_guard()
del _install_guard

__all__: list[str] = []