"""Closed product-owned source registry for operator-selectable product sources.

The registry maps a stable operator-facing machine identifier to an already-supported
canonical source factory specification.  It does not load or execute the factory and
does not grant runtime authority.  The canonical product composition root must still
pass the returned factory specification through ``product_entrypoint._validated_source``.
"""

from __future__ import annotations

from dataclasses import dataclass
import keyword
import re


_SOURCE_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z", re.ASCII)


class OperatorSourceRegistryError(ValueError):
    """An operator source identifier is malformed or absent from product truth."""


def _canonical_source_id(value: object) -> str:
    if type(value) is not str:
        raise OperatorSourceRegistryError("source_id must be text")
    if not value or len(value) > 64 or _SOURCE_ID_RE.fullmatch(value) is None:
        raise OperatorSourceRegistryError(
            "source_id is not a canonical product source identifier"
        )
    return value


def _factory_spec(value: object) -> str:
    if type(value) is not str or value.strip() != value or value.count(":") != 1:
        raise OperatorSourceRegistryError("factory_spec is not canonical module:function text")
    module_name, function_name = value.split(":", 1)
    if (
        not module_name
        or not function_name
        or any(part == "" for part in module_name.split("."))
        or not all(
            part.isidentifier() and part.isascii() and not keyword.iskeyword(part)
            for part in module_name.split(".")
        )
        or not function_name.isidentifier()
        or not function_name.isascii()
        or keyword.iskeyword(function_name)
    ):
        raise OperatorSourceRegistryError("factory_spec is not canonical module:function text")
    return value


def _provider_source_id(value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise OperatorSourceRegistryError("expected_provider_source_id must be non-empty text")
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise OperatorSourceRegistryError(
            "expected_provider_source_id must be printable ASCII without whitespace"
        )
    return value


@dataclass(frozen=True, slots=True)
class ProductSourceRegistryEntry:
    """One product-shipped source binding; the value itself is not runtime authority."""

    source_id: str
    factory_spec: str
    expected_provider_source_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _canonical_source_id(self.source_id))
        object.__setattr__(self, "factory_spec", _factory_spec(self.factory_spec))
        object.__setattr__(
            self,
            "expected_provider_source_id",
            _provider_source_id(self.expected_provider_source_id),
        )

    @property
    def runtime_authorized(self) -> bool:
        """Registry lookup alone can never authorize runtime construction."""

        return False


_PRODUCT_SOURCE_ENTRIES = (
    ProductSourceRegistryEntry(
        source_id="parlayapi-table-tennis",
        factory_spec="autosport.product_source:create_parlay_product_source",
        expected_provider_source_id="parlayapi:table_tennis",
    ),
)

if len({entry.source_id for entry in _PRODUCT_SOURCE_ENTRIES}) != len(
    _PRODUCT_SOURCE_ENTRIES
):
    raise RuntimeError("duplicate canonical product source_id")
if len({entry.factory_spec for entry in _PRODUCT_SOURCE_ENTRIES}) != len(
    _PRODUCT_SOURCE_ENTRIES
):
    raise RuntimeError("duplicate canonical product source factory_spec")


def list_product_source_entries() -> tuple[ProductSourceRegistryEntry, ...]:
    """Return the immutable product-shipped source options in stable order."""

    return _PRODUCT_SOURCE_ENTRIES


def resolve_product_source_entry(source_id: str) -> ProductSourceRegistryEntry:
    """Resolve one exact stable ID with no aliasing, normalization, or fallback."""

    canonical = _canonical_source_id(source_id)
    for entry in _PRODUCT_SOURCE_ENTRIES:
        if entry.source_id == canonical:
            return entry
    raise OperatorSourceRegistryError("source_id is not registered by this product build")


def resolve_product_source_factory_spec(source_id: str) -> str:
    """Return product-owned factory text for later canonical runtime validation."""

    return resolve_product_source_entry(source_id).factory_spec
