"""Seal the stdlib JSON dispatch used by provider-sport evidence parsing.

This is a same-lineage extension of the Wave N curation fence.  It does not create a
parser or mapping authority: it snapshots the concrete stdlib dispatch graph already
used by the canonical parser and rejects mutation before positive registration can
execute it.
"""

from __future__ import annotations

from . import provider_sport_mapping as _mapping


def _install_guard() -> None:
    registry_type = _mapping.ProviderSportMappingRegistry
    current_register = registry_type.register_evidence
    if getattr(current_register, "_product_json_dispatch_guard", False):
        return

    canonical_json = _mapping.json
    canonical_loads = canonical_json.loads
    canonical_loads_code = getattr(canonical_loads, "__code__", None)
    canonical_decoder = canonical_json.JSONDecoder
    decoder_init = canonical_decoder.__init__
    decoder_decode = canonical_decoder.decode
    decoder_raw_decode = canonical_decoder.raw_decode
    decoder_init_code = getattr(decoder_init, "__code__", None)
    decoder_decode_code = getattr(decoder_decode, "__code__", None)
    decoder_raw_decode_code = getattr(decoder_raw_decode, "__code__", None)
    canonical_error = _mapping.ProviderSportMappingError

    def require_json_authority() -> None:
        if _mapping.json is not canonical_json or canonical_json.loads is not canonical_loads:
            raise canonical_error("canonical evidence parser JSON authority changed")
        if canonical_loads_code is not None and getattr(canonical_loads, "__code__", None) is not canonical_loads_code:
            raise canonical_error("canonical evidence parser JSON executable authority changed")
        if canonical_json.JSONDecoder is not canonical_decoder:
            raise canonical_error("canonical evidence parser JSON decoder authority changed")
        if (
            canonical_decoder.__init__ is not decoder_init
            or canonical_decoder.decode is not decoder_decode
            or canonical_decoder.raw_decode is not decoder_raw_decode
        ):
            raise canonical_error("canonical evidence parser JSON decoder dispatch changed")
        if (
            (decoder_init_code is not None and getattr(decoder_init, "__code__", None) is not decoder_init_code)
            or (decoder_decode_code is not None and getattr(decoder_decode, "__code__", None) is not decoder_decode_code)
            or (decoder_raw_decode_code is not None and getattr(decoder_raw_decode, "__code__", None) is not decoder_raw_decode_code)
        ):
            raise canonical_error("canonical evidence parser JSON decoder executable authority changed")

    def register_evidence(self, source_snapshot_bytes: bytes):
        if registry_type.register_evidence is not register_evidence:
            raise canonical_error("provider sport JSON dispatch guard authority changed")
        require_json_authority()
        binding = current_register(self, source_snapshot_bytes)
        require_json_authority()
        return binding

    setattr(register_evidence, "_product_json_dispatch_guard", True)
    registry_type.register_evidence = register_evidence


_install_guard()
del _install_guard

__all__: list[str] = []
