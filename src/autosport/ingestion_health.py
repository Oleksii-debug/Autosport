from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import stat
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from types import FunctionType
from typing import BinaryIO

from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicAuthorityRecoveryRequiredError,
    MonotonicAuthorityRollbackError,
    MonotonicWorkspaceAuthority,
)
from . import secret_redaction as _secret_redaction


_ALLOWED_HEALTH_STATUSES = frozenset({"unknown", "healthy", "degraded", "failed"})
_COUNTER_FIELDS = (
    "poll_count",
    "total_received",
    "total_accepted",
    "total_rejected",
    "total_failures",
    "consecutive_failures",
    "consecutive_failure_kind_count",
)
_FAILURE_KIND_PROVIDER_UNAVAILABLE = "provider_unavailable"
_FAILURE_KIND_PROVIDER_OR_VALIDATION = "provider_or_validation"
_ALLOWED_FAILURE_KINDS = frozenset(
    {
        _FAILURE_KIND_PROVIDER_UNAVAILABLE,
        _FAILURE_KIND_PROVIDER_OR_VALIDATION,
    }
)
_SCHEMA_V1 = 1
_SCHEMA_V2 = 2
_SCHEMA_V3 = 3
_SCHEMA_V4 = 4
_HISTORY_ENTRY_V2_FIELDS = frozenset({"recorded_at", "state"})
_HISTORY_ENTRY_V3_FIELDS = frozenset({"recorded_at", "transition_order", "state"})
_SOURCE_HEALTH_AUTHORITY_DOMAIN = "autosport.source-health-store.v1"
_DURABLE_FAILURE_FALLBACK = "BaseException: exception details unavailable"


def _build_durable_failure_renderer():
    secret_module = _secret_redaction
    canonical_renderer = secret_module.safe_exception_text
    canonical_globals = canonical_renderer.__globals__
    fallback = _DURABLE_FAILURE_FALLBACK

    function_witness = tuple(
        (name, value, value.__code__)
        for name, value in canonical_globals.items()
        if type(value) is FunctionType
        and getattr(value, "__module__", None) == secret_module.__name__
    )
    referenced_names = frozenset(
        name
        for _function_name, function, _code in function_witness
        for name in function.__code__.co_names
        if name in canonical_globals
    )
    binding_witness = tuple(
        (name, canonical_globals[name])
        for name in sorted(referenced_names)
    )
    mutable_binding_witness = tuple(
        (name, tuple(sorted(value.items())))
        for name, value in binding_witness
        if type(value) is dict
    )

    def authority_current() -> bool:
        if secret_module.safe_exception_text is not canonical_renderer:
            return False
        for name, function, code in function_witness:
            if canonical_globals.get(name) is not function:
                return False
            if function.__code__ is not code:
                return False
        for name, value in binding_witness:
            if canonical_globals.get(name) is not value:
                return False
        for name, expected_items in mutable_binding_witness:
            value = canonical_globals.get(name)
            if type(value) is not dict:
                return False
            if tuple(sorted(value.items())) != expected_items:
                return False
        return True

    def render(exc: BaseException) -> str:
        if not authority_current():
            return fallback
        try:
            rendered = canonical_renderer(exc)
        except BaseException:
            return fallback
        if not authority_current() or type(rendered) is not str or not rendered:
            return fallback
        return rendered

    return render


_DURABLE_FAILURE_RENDERER = _build_durable_failure_renderer()
del _build_durable_failure_renderer


def _build_record_failure_method(renderer):
    record_failure = _build_record_failure_method(_DURABLE_FAILURE_RENDERER)

    def _writer_guard(self) -> _SourceHealthWriterLock:
        self._assert_persistence_authority()
        return _SourceHealthWriterLock(self._lock_path_authority)

    def _upgrade_to_v4(self, raw: dict) -> dict:
        if raw["schema_version"] == _SCHEMA_V4:
            return raw
        upgraded = {"schema_version": _SCHEMA_V4, "sources": {}, "history": {}}

        if raw["schema_version"] == _SCHEMA_V1:
            for source_id, payload in raw["sources"].items():
                state = self._state_from_payload(payload)
                normalized = self._payload(state)
                upgraded["sources"][source_id] = normalized
                recorded_at = self._transition_at(state)
                if recorded_at is None:
                    raise ValueError(
                        "persisted non-pristine source health requires transition timestamp"
                    )
                upgraded["history"][source_id] = [
                    {
                        "recorded_at": recorded_at,
                        "transition_order": 1,
                        "state": normalized,
                    }
                ]
            return upgraded

        for source_id, payload in raw["sources"].items():
            upgraded["sources"][source_id] = self._payload(
                self._state_from_payload(payload, normalize_failed_flags=False)
            )
            entries: list[dict] = []
            for index, entry in enumerate(raw["history"][source_id], start=1):
                transition_order = (
                    entry["transition_order"]
                    if raw["schema_version"] == _SCHEMA_V3
                    else index
                )
                entries.append(
                    {
                        "recorded_at": entry["recorded_at"],
                        "transition_order": transition_order,
                        "state": self._payload(
                            self._state_from_payload(
                                entry["state"], normalize_failed_flags=False
                            )
                        ),
                    }
                )
            upgraded["history"][source_id] = entries
        return upgraded

    def _put(self, state: SourceHealthState, *, recorded_at: str) -> None:
        state.validate()
        recorded = parse_source_timestamp(recorded_at)
        transition_at = self._transition_at(state)
        if transition_at is None or parse_source_timestamp(transition_at) != recorded:
            raise ValueError("source health transition timestamp mismatch")

        raw = self._upgrade_to_v4(self._read())
        entries = raw["history"].setdefault(state.source_id, [])
        if entries and parse_source_timestamp(entries[-1]["recorded_at"]) > recorded:
            raise ValueError("source health transitions cannot move backwards in evidence time")

        payload = self._payload(state)
        transition_order = entries[-1]["transition_order"] + 1 if entries else 1
        entries.append(
            {
                "recorded_at": recorded_at,
                "transition_order": transition_order,
                "state": payload,
            }
        )
        raw["sources"][state.source_id] = payload
        self._write(raw)

    @staticmethod
    def _validate_persisted_state(
        source_id: str,
        payload: object,
        *,
        schema_version: int,
    ) -> None:
        _validate_source_id(source_id)
        expected_state_fields = (
            _SOURCE_STATE_FIELDS
            if schema_version == _SCHEMA_V4
            else _LEGACY_SOURCE_STATE_FIELDS
        )
        if not isinstance(payload, dict) or set(payload) != expected_state_fields:
            raise ValueError("invalid source health state fields")
        if payload.get("source_id") != source_id:
            raise ValueError("source health state identity mismatch")
        if not isinstance(payload.get("quality_flags"), list):
            raise ValueError("persisted quality_flags must be a JSON array")
        value = dict(payload)
        value.setdefault("last_failure_kind", None)
        value.setdefault("consecutive_failure_kind_count", 0)
        value["quality_flags"] = tuple(value["quality_flags"])
        SourceHealthState(**value)

    def _read_snapshot(
        self,
        *,
        verify_authority: bool = True,
    ) -> tuple[dict, str]:
        self._assert_persistence_authority()
        self._assert_target_shape()
        try:
            raw_bytes = self._path_authority.read_bytes()
            raw = json.loads(
                raw_bytes.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("invalid source health store") from exc

        schema_version = raw.get("schema_version") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version not in {_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4}
            or not isinstance(raw.get("sources"), dict)
        ):
            raise ValueError("invalid source health store")

        expected_fields = (
            {"schema_version", "sources"}
            if schema_version == _SCHEMA_V1
            else {"schema_version", "sources", "history"}
        )
        if set(raw) != expected_fields:
            raise ValueError("invalid source health store")
        if schema_version in {_SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4} and not isinstance(
            raw.get("history"), dict
        ):
            raise ValueError("invalid source health store")

        try:
            for source_id, payload in raw["sources"].items():
                self._validate_persisted_state(
                    source_id,
                    payload,
                    schema_version=schema_version,
                )

            if schema_version in {_SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4}:
                if set(raw["history"]) != set(raw["sources"]):
                    raise ValueError("source health history/projection identity mismatch")
                for source_id, entries in raw["history"].items():
                    if not isinstance(entries, list) or not entries:
                        raise ValueError("source health history must be a non-empty array")
                    previous_recorded: datetime | None = None
                    previous_order = 0
                    for entry in entries:
                        expected_entry_fields = (
                            _HISTORY_ENTRY_V2_FIELDS
                            if schema_version == _SCHEMA_V2
                            else _HISTORY_ENTRY_V3_FIELDS
                        )
                        if not isinstance(entry, dict) or set(entry) != expected_entry_fields:
                            raise ValueError("invalid source health history entry")
                        recorded_at = parse_source_timestamp(entry["recorded_at"])
                        if schema_version == _SCHEMA_V2:
                            if previous_recorded is not None and recorded_at <= previous_recorded:
                                raise ValueError("source health history is not strictly increasing")
                        else:
                            order = entry["transition_order"]
                            if (
                                isinstance(order, bool)
                                or not isinstance(order, int)
                                or order != previous_order + 1
                            ):
                                raise ValueError("source health transition order is not contiguous")
                            if previous_recorded is not None and recorded_at < previous_recorded:
                                raise ValueError("source health history evidence time moved backwards")
                            previous_order = order
                        previous_recorded = recorded_at
                        self._validate_persisted_state(
                            source_id,
                            entry["state"],
                            schema_version=schema_version,
                        )
                        state = self._state_from_payload(
                            entry["state"], normalize_failed_flags=False
                        )
                        transition_at = self._transition_at(state)
                        if (
                            transition_at is None
                            or parse_source_timestamp(transition_at) != recorded_at
                        ):
                            raise ValueError("source health history timestamp mismatch")
                    if entries[-1]["state"] != raw["sources"][source_id]:
                        raise ValueError("source health latest projection/history mismatch")
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid source health state/history") from exc
        digest = self._sha256_bytes(raw_bytes)
        if verify_authority:
            self._verify_authority_current(digest)
        return raw, digest

    def _read(self, *, verify_authority: bool = True) -> dict:
        raw, _ = self._read_snapshot(verify_authority=verify_authority)
        return raw

    def _write(self, raw: dict) -> None:
        self._assert_persistence_authority()
        temporary = self._temporary_path_authority
        try:
            if temporary.exists() or temporary.is_symlink():
                raise RuntimeError(
                    "source health temporary persistence path already exists"
                )
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(temporary, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n", closefd=True) as handle:
                info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_nlink", 1) != 1
                ):
                    raise RuntimeError(
                        "source health temporary persistence path must be one regular file"
                    )
                json.dump(raw, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            intended = self._sha256_bytes(temporary.read_bytes())
            observed = self._current_state_sha256()
            authority = self._recover_or_bootstrap_authority(observed)
            binding = self._authority_binding(observed, intended, kind="PUBLISH")
            tx_id = self._next_authority_tx_id(
                authority,
                observed,
                intended,
                binding,
            )
            authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=observed,
                intended_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
            self._assert_persistence_authority()
            os.replace(temporary, self._path_authority)
            self._sync_parent_directory()
            published = self._current_state_sha256()
            if published != intended:
                raise RuntimeError(
                    "published source health bytes do not match prepared authority digest"
                )
            authority.commit(
                tx_id=tx_id,
                observed_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
            self._assert_persistence_authority()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


# SourceHealthStore.record_failure retains the sealed renderer in a lexical cell.
del _DURABLE_FAILURE_RENDERER
del _DURABLE_FAILURE_FALLBACK
del _build_record_failure_method
del _secret_redaction
