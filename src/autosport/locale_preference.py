from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .localization import DEFAULT_LOCALE, catalog


_SCHEMA_VERSION = 1
_REQUIRED_KEYS = frozenset({"schema_version", "locale", "content_sha256"})


@dataclass(frozen=True, slots=True)
class LocalePreference:
    """Validated locale preference loaded from durable storage or the product default."""

    locale: str
    persisted: bool


def _validate_supported_locale(locale: object) -> str:
    if type(locale) is not str or not locale:
        raise ValueError("locale preference locale must be a non-empty string")
    try:
        catalog(locale)
    except ValueError as exc:
        raise ValueError(f"unsupported locale preference: {locale!r}") from exc
    return locale


def _canonical_core_bytes(*, schema_version: int, locale: str) -> bytes:
    core = {"locale": locale, "schema_version": schema_version}
    return json.dumps(
        core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _content_sha256(*, schema_version: int, locale: str) -> str:
    return hashlib.sha256(
        _canonical_core_bytes(schema_version=schema_version, locale=locale)
    ).hexdigest()


def _canonical_document_bytes(locale: str) -> bytes:
    locale = _validate_supported_locale(locale)
    document = {
        "content_sha256": _content_sha256(
            schema_version=_SCHEMA_VERSION,
            locale=locale,
        ),
        "locale": locale,
        "schema_version": _SCHEMA_VERSION,
    }
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"locale preference contains duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _decode_document(raw: bytes) -> str:
    try:
        parsed: Any = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("locale preference is not valid UTF-8 JSON") from exc

    if type(parsed) is not dict:
        raise ValueError("locale preference document must be a JSON object")
    if set(parsed) != _REQUIRED_KEYS:
        raise ValueError(
            "locale preference document keys must be exactly "
            f"{sorted(_REQUIRED_KEYS)!r}"
        )

    schema_version = parsed["schema_version"]
    if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
        raise ValueError(
            f"unsupported locale preference schema_version: {schema_version!r}"
        )

    locale = _validate_supported_locale(parsed["locale"])
    content_sha256 = parsed["content_sha256"]
    if type(content_sha256) is not str or len(content_sha256) != 64:
        raise ValueError("locale preference content_sha256 must be a SHA-256 hex string")
    try:
        int(content_sha256, 16)
    except ValueError as exc:
        raise ValueError(
            "locale preference content_sha256 must be a SHA-256 hex string"
        ) from exc

    expected = _content_sha256(schema_version=schema_version, locale=locale)
    if not hmac.compare_digest(content_sha256, expected):
        raise ValueError("locale preference content hash mismatch")
    return locale


def load_locale_preference(path: str | Path) -> LocalePreference:
    """Load one strict durable preference; absence means the canonical uk-UA default."""

    preference_path = Path(path)
    try:
        raw = preference_path.read_bytes()
    except FileNotFoundError:
        return LocalePreference(locale=DEFAULT_LOCALE, persisted=False)

    return LocalePreference(locale=_decode_document(raw), persisted=True)


def _fsync_parent_directory(parent: Path) -> None:
    """Best-effort directory durability where the host exposes directory fsync."""

    flags = getattr(os, "O_RDONLY", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not directory_flag:
        return

    fd: int | None = None
    try:
        fd = os.open(parent, flags | directory_flag)
        os.fsync(fd)
    except OSError:
        # Windows does not expose POSIX directory-fsync semantics. File fsync
        # before atomic replace is still mandatory and is performed below.
        return
    finally:
        if fd is not None:
            os.close(fd)


def save_locale_preference(path: str | Path, locale: str) -> LocalePreference:
    """Atomically publish one validated locale preference.

    The final file is canonical UTF-8 JSON. Publication writes a sibling
    temporary file, flushes and fsyncs it, then replaces the target atomically.
    Existing valid state remains untouched if publication fails before replace.
    """

    preference_path = Path(path)
    document = _canonical_document_bytes(locale)
    preference_path.parent.mkdir(parents=True, exist_ok=True)

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=preference_path.parent,
            prefix=f".{preference_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temp_path, preference_path)
        temp_path = None
        _fsync_parent_directory(preference_path.parent)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    return LocalePreference(locale=_validate_supported_locale(locale), persisted=True)
