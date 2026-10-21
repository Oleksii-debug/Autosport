from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from autosport import locale_preference
from autosport.localization import DEFAULT_LOCALE


def _write_document(
    path: Path,
    *,
    locale: object = DEFAULT_LOCALE,
    schema_version: object = 1,
    content_sha256: object | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    core = {"locale": locale, "schema_version": schema_version}
    if content_sha256 is None:
        core_bytes = json.dumps(
            core,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        content_sha256 = hashlib.sha256(core_bytes).hexdigest()
    document: dict[str, object] = {
        **core,
        "content_sha256": content_sha256,
    }
    if extra:
        document.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def test_missing_preference_uses_canonical_ukrainian_default(tmp_path: Path) -> None:
    path = tmp_path / "preferences" / "locale.json"

    preference = locale_preference.load_locale_preference(path)

    assert preference.locale == "uk-UA"
    assert preference.locale == DEFAULT_LOCALE
    assert preference.persisted is False
    assert not path.exists()


def test_save_and_restart_reload_exact_preference(tmp_path: Path) -> None:
    path = tmp_path / "Дані Автоспорт" / "preferences" / "locale.json"

    written = locale_preference.save_locale_preference(path, DEFAULT_LOCALE)
    reloaded = locale_preference.load_locale_preference(path)

    assert written == locale_preference.LocalePreference(
        locale=DEFAULT_LOCALE,
        persisted=True,
    )
    assert reloaded == written
    assert path.read_bytes().endswith(b"\n")


def test_saved_bytes_are_deterministic_across_republication(tmp_path: Path) -> None:
    path = tmp_path / "locale.json"

    locale_preference.save_locale_preference(path, DEFAULT_LOCALE)
    first = path.read_bytes()
    locale_preference.save_locale_preference(path, DEFAULT_LOCALE)
    second = path.read_bytes()

    assert first == second
    document = json.loads(first.decode("utf-8"))
    assert list(sorted(document)) == [
        "content_sha256",
        "locale",
        "schema_version",
    ]
    assert document["locale"] == DEFAULT_LOCALE
    assert document["schema_version"] == 1


def test_atomic_publication_uses_sibling_temp_fsync_then_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "preferences" / "locale.json"
    real_fsync = locale_preference.os.fsync
    real_replace = locale_preference.os.replace
    events: list[tuple[str, object]] = []

    def recording_fsync(fd: int) -> None:
        events.append(("fsync", fd))
        real_fsync(fd)

    def recording_replace(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        events.append(("replace", (source_path, destination_path)))
        assert source_path.parent == target.parent
        assert destination_path == target
        assert source_path.name.startswith(f".{target.name}.")
        assert source_path.suffix == ".tmp"
        assert (
            json.loads(source_path.read_text(encoding="utf-8"))["locale"]
            == DEFAULT_LOCALE
        )
        real_replace(source, destination)

    monkeypatch.setattr(locale_preference.os, "fsync", recording_fsync)
    monkeypatch.setattr(locale_preference.os, "replace", recording_replace)

    locale_preference.save_locale_preference(target, DEFAULT_LOCALE)

    kinds = [kind for kind, _ in events]
    assert "fsync" in kinds
    assert "replace" in kinds
    assert kinds.index("fsync") < kinds.index("replace")


def test_replace_failure_keeps_existing_file_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "preferences" / "locale.json"
    locale_preference.save_locale_preference(target, DEFAULT_LOCALE)
    original = target.read_bytes()

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(locale_preference.os, "replace", fail_replace)

    with pytest.raises(OSError, match="synthetic replace failure"):
        locale_preference.save_locale_preference(target, DEFAULT_LOCALE)

    assert target.read_bytes() == original
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))


@pytest.mark.parametrize(
    ("raw_bytes", "message"),
    [
        (b"not json", "valid UTF-8 JSON"),
        (b"[]", "must be a JSON object"),
        (
            json.dumps(
                {
                    "schema_version": True,
                    "locale": DEFAULT_LOCALE,
                    "content_sha256": "0" * 64,
                }
            ).encode("utf-8"),
            "schema_version",
        ),
    ],
)
def test_existing_malformed_preference_never_silently_falls_back(
    tmp_path: Path,
    raw_bytes: bytes,
    message: str,
) -> None:
    path = tmp_path / "locale.json"
    path.write_bytes(raw_bytes)

    with pytest.raises(ValueError, match=message):
        locale_preference.load_locale_preference(path)


def test_duplicate_json_keys_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "locale.json"
    path.write_text(
        (
            '{"content_sha256":"'
            + ("0" * 64)
            + '","locale":"uk-UA","locale":"uk-UA","schema_version":1}\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        locale_preference.load_locale_preference(path)


def test_unknown_document_keys_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "locale.json"
    _write_document(path, extra={"future_flag": True})

    with pytest.raises(ValueError, match="keys must be exactly"):
        locale_preference.load_locale_preference(path)


def test_content_tamper_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "locale.json"
    _write_document(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["content_sha256"] = "0" * 64
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="content hash mismatch"):
        locale_preference.load_locale_preference(path)


def test_unsupported_locale_is_rejected_on_save_and_load(tmp_path: Path) -> None:
    path = tmp_path / "locale.json"

    with pytest.raises(ValueError, match="unsupported locale preference"):
        locale_preference.save_locale_preference(path, "en-US")
    assert not path.exists()

    _write_document(path, locale="en-US")
    with pytest.raises(ValueError, match="unsupported locale preference"):
        locale_preference.load_locale_preference(path)


def test_non_string_locale_and_invalid_hash_shape_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "locale.json"
    _write_document(path, locale=123, content_sha256="0" * 64)
    with pytest.raises(ValueError, match="locale must be a non-empty string"):
        locale_preference.load_locale_preference(path)

    _write_document(path, content_sha256="xyz")
    with pytest.raises(ValueError, match="SHA-256 hex string"):
        locale_preference.load_locale_preference(path)
