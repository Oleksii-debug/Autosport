from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from .integrity import durable_path_lock


ConfigMapping = Mapping[str, Any]
MigrationTransform = Callable[[dict[str, Any]], dict[str, Any]]
ConfigValidator = Callable[[ConfigMapping], None]


class OperatorConfigMigrationError(RuntimeError):
    """Base class for fail-closed persisted operator-configuration migration."""


class ConfigSchemaError(OperatorConfigMigrationError):
    """Persisted operator configuration is malformed or has an unsupported schema."""


class ConfigMigrationChainError(OperatorConfigMigrationError):
    """The requested schema transition does not have one explicit step per version."""


class ConfigPublicationUncertainError(OperatorConfigMigrationError):
    """Atomic replace completed but exact destination readback could not be proven."""


class ConfigBackupIntegrityError(OperatorConfigMigrationError):
    """A deterministic rollback backup is missing, substituted, or corrupted."""


@dataclass(frozen=True)
class ConfigMigrationStep:
    """One explicit from-version -> from-version+1 migration step.

    ``mutable_keys`` is the complete set of top-level keys the step may add,
    remove, or change. Every other pre-existing top-level field is preserved
    byte-semantically at the JSON-value level, which prevents an upgrade from
    silently dropping unknown extension fields.
    """

    from_version: int
    to_version: int
    transform: MigrationTransform = field(repr=False, compare=False)
    mutable_keys: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if isinstance(self.from_version, bool) or not isinstance(self.from_version, int):
            raise TypeError("from_version must be an integer")
        if isinstance(self.to_version, bool) or not isinstance(self.to_version, int):
            raise TypeError("to_version must be an integer")
        if self.from_version < 1 or self.to_version != self.from_version + 1:
            raise ValueError("each migration step must advance exactly one schema version")
        if not callable(self.transform):
            raise TypeError("transform must be callable")
        normalized = frozenset(self.mutable_keys)
        if any(type(key) is not str or not key for key in normalized):
            raise ValueError("mutable_keys must contain non-empty strings")
        if "schema_version" in normalized:
            raise ValueError("schema_version is owned by the migration engine")
        object.__setattr__(self, "mutable_keys", normalized)


@dataclass(frozen=True)
class ConfigMigrationPreview:
    from_version: int
    to_version: int
    before_sha256: str
    after_sha256: str
    changed: bool


@dataclass(frozen=True)
class ConfigMigrationJournal:
    status: str
    from_version: int
    to_version: int
    before_sha256: str
    after_sha256: str
    backup_sha256: str | None

    def __post_init__(self) -> None:
        if self.status not in {"NO_CHANGE", "MIGRATED", "RESTORED"}:
            raise ValueError("invalid migration journal status")

    def as_public_record(self) -> Mapping[str, object]:
        """Return a log-safe record containing no paths or configuration values."""
        return MappingProxyType(
            {
                "status": self.status,
                "from_version": self.from_version,
                "to_version": self.to_version,
                "before_sha256": self.before_sha256,
                "after_sha256": self.after_sha256,
                "backup_sha256": self.backup_sha256,
            }
        )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigSchemaError("operator config contains duplicate JSON object keys")
        result[key] = value
    return result


def _reject_nonfinite(token: str) -> None:
    raise ConfigSchemaError("operator config contains a non-finite JSON number")


def _decode_config(data: bytes) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigSchemaError("operator config must be valid UTF-8 JSON") from exc
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_nonfinite,
        )
    except ConfigSchemaError:
        raise
    except json.JSONDecodeError as exc:
        raise ConfigSchemaError("operator config must be valid UTF-8 JSON") from exc
    if type(payload) is not dict:
        raise ConfigSchemaError("operator config root must be a JSON object")
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ConfigSchemaError("operator config schema_version must be a positive integer")
    return payload


def _encode_config(payload: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            dict(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigSchemaError("migrated operator config is not canonical JSON data") from exc
    return (text + "\n").encode("utf-8")


def _read_regular_bytes(path: Path, *, kind: str) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ConfigSchemaError(f"{kind} does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigSchemaError(f"{kind} must be a regular non-symlink file")
    return path.read_bytes()


def _changed_top_level_keys(before: Mapping[str, Any], after: Mapping[str, Any]) -> set[str]:
    changed: set[str] = set()
    for key in set(before) | set(after):
        if key == "schema_version":
            continue
        if key not in before or key not in after or before[key] != after[key]:
            changed.add(key)
    return changed


def _backup_path(destination: Path, *, from_version: int, digest: str) -> Path:
    return destination.with_name(
        f".{destination.name}.backup-v{from_version}-{digest}.json"
    )


def _fsync_parent(path: Path) -> None:
    # Windows does not provide portable directory-fsync through Python's stdlib.
    # File bytes themselves are fsync'd on all platforms; POSIX also fsyncs the
    # containing directory so the created/replaced name is made durable.
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path.parent, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_exact_backup(path: Path, data: bytes, *, expected_sha256: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            existing = _read_regular_bytes(path, kind="operator config backup")
        except ConfigSchemaError as exc:
            raise ConfigBackupIntegrityError(str(exc)) from exc
        if _sha256_bytes(existing) != expected_sha256 or existing != data:
            raise ConfigBackupIntegrityError(
                "existing operator config backup does not match its bound digest"
            )
        return
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        verified = _read_regular_bytes(path, kind="operator config backup")
        if _sha256_bytes(verified) != expected_sha256 or verified != data:
            raise ConfigBackupIntegrityError(
                "operator config backup readback does not match original bytes"
            )
        _fsync_parent(path)
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _stage_bytes(destination: Path, data: bytes, *, expected_sha256: str) -> Path:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.migration-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        staged = _read_regular_bytes(temporary, kind="operator config staged file")
        if _sha256_bytes(staged) != expected_sha256 or staged != data:
            raise OperatorConfigMigrationError(
                "staged operator config readback does not match intended bytes"
            )
        return temporary
    except BaseException:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise


def _atomic_publish(destination: Path, data: bytes, *, expected_sha256: str) -> None:
    temporary = _stage_bytes(destination, data, expected_sha256=expected_sha256)
    replaced = False
    try:
        os.replace(temporary, destination)
        replaced = True
        temporary = None
        try:
            _fsync_parent(destination)
            published = _read_regular_bytes(destination, kind="operator config")
        except BaseException as exc:
            raise ConfigPublicationUncertainError(
                "operator config replace completed but durability/readback is uncertain"
            ) from exc
        if _sha256_bytes(published) != expected_sha256 or published != data:
            raise ConfigPublicationUncertainError(
                "operator config replace completed but exact destination bytes are uncertain"
            )
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        # `replaced` is intentionally not used to roll the destination back.
        # After a completed atomic replace, a verifier failure is an uncertainty
        # state that must be reconciled against the still-valid immutable backup.
        _ = replaced


class OperatorConfigMigrator:
    """Preview and atomically migrate one persisted operator configuration."""

    def __init__(
        self,
        *,
        target_version: int,
        steps: Iterable[ConfigMigrationStep],
        validator: ConfigValidator | None = None,
    ) -> None:
        if isinstance(target_version, bool) or not isinstance(target_version, int):
            raise TypeError("target_version must be an integer")
        if target_version < 1:
            raise ValueError("target_version must be positive")
        step_map: dict[int, ConfigMigrationStep] = {}
        for step in steps:
            if not isinstance(step, ConfigMigrationStep):
                raise TypeError("steps must contain ConfigMigrationStep values")
            if step.from_version in step_map:
                raise ValueError("duplicate migration step from_version")
            step_map[step.from_version] = step
        if validator is not None and not callable(validator):
            raise TypeError("validator must be callable")
        self._target_version = target_version
        self._steps = MappingProxyType(step_map)
        self._validator = validator

    @property
    def target_version(self) -> int:
        return self._target_version

    def _validate(self, payload: Mapping[str, Any]) -> None:
        if self._validator is not None:
            self._validator(MappingProxyType(deepcopy(dict(payload))))

    def _migrate_payload(self, source: Mapping[str, Any]) -> dict[str, Any]:
        current = deepcopy(dict(source))
        current_version = current["schema_version"]
        assert isinstance(current_version, int) and not isinstance(current_version, bool)
        if current_version > self._target_version:
            raise ConfigMigrationChainError(
                "operator config downgrade is not permitted"
            )
        self._validate(current)
        while current_version < self._target_version:
            step = self._steps.get(current_version)
            if step is None or step.to_version != current_version + 1:
                raise ConfigMigrationChainError(
                    "operator config migration requires an explicit one-version step"
                )
            before = deepcopy(current)
            try:
                candidate = step.transform(deepcopy(current))
            except OperatorConfigMigrationError:
                raise
            except Exception as exc:
                raise OperatorConfigMigrationError(
                    "operator config migration step failed"
                ) from exc
            if type(candidate) is not dict:
                raise ConfigSchemaError("operator config migration step must return a dict")
            changed = _changed_top_level_keys(before, candidate)
            unauthorized = changed - set(step.mutable_keys)
            if unauthorized:
                raise ConfigSchemaError(
                    "operator config migration changed undeclared top-level fields"
                )
            candidate["schema_version"] = step.to_version
            current = candidate
            current_version = step.to_version
            self._validate(current)
        return current

    def _plan_from_bytes(self, original: bytes) -> tuple[ConfigMigrationPreview, bytes]:
        payload = _decode_config(original)
        before_sha256 = _sha256_bytes(original)
        migrated = self._migrate_payload(payload)
        if payload["schema_version"] == self._target_version:
            candidate = original
        else:
            candidate = _encode_config(migrated)
            # Re-parse the exact serialised bytes before publication so the
            # validator and schema engine agree on what will be reopened later.
            reopened = _decode_config(candidate)
            self._validate(reopened)
        after_sha256 = _sha256_bytes(candidate)
        return (
            ConfigMigrationPreview(
                from_version=payload["schema_version"],
                to_version=self._target_version,
                before_sha256=before_sha256,
                after_sha256=after_sha256,
                changed=candidate != original,
            ),
            candidate,
        )

    def preview(self, path: str | Path) -> ConfigMigrationPreview:
        destination = Path(path)
        original = _read_regular_bytes(destination, kind="operator config")
        preview, _candidate = self._plan_from_bytes(original)
        return preview

    def migrate(self, path: str | Path) -> ConfigMigrationJournal:
        destination = Path(path)
        with durable_path_lock(destination):
            original = _read_regular_bytes(destination, kind="operator config")
            preview, candidate = self._plan_from_bytes(original)
            if not preview.changed:
                return ConfigMigrationJournal(
                    status="NO_CHANGE",
                    from_version=preview.from_version,
                    to_version=preview.to_version,
                    before_sha256=preview.before_sha256,
                    after_sha256=preview.after_sha256,
                    backup_sha256=None,
                )

            backup = _backup_path(
                destination,
                from_version=preview.from_version,
                digest=preview.before_sha256,
            )
            _write_exact_backup(
                backup,
                original,
                expected_sha256=preview.before_sha256,
            )
            _atomic_publish(
                destination,
                candidate,
                expected_sha256=preview.after_sha256,
            )
            return ConfigMigrationJournal(
                status="MIGRATED",
                from_version=preview.from_version,
                to_version=preview.to_version,
                before_sha256=preview.before_sha256,
                after_sha256=preview.after_sha256,
                backup_sha256=preview.before_sha256,
            )

    def restore(
        self,
        path: str | Path,
        *,
        backup_sha256: str,
        backup_from_version: int,
    ) -> ConfigMigrationJournal:
        if (
            type(backup_sha256) is not str
            or len(backup_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in backup_sha256)
        ):
            raise ConfigBackupIntegrityError("backup_sha256 must be canonical lowercase SHA-256")
        if (
            isinstance(backup_from_version, bool)
            or not isinstance(backup_from_version, int)
            or backup_from_version < 1
        ):
            raise ConfigBackupIntegrityError("backup_from_version must be a positive integer")

        destination = Path(path)
        with durable_path_lock(destination):
            current = _read_regular_bytes(destination, kind="operator config")
            current_payload = _decode_config(current)
            self._validate(current_payload)
            current_sha256 = _sha256_bytes(current)

            backup = _backup_path(
                destination,
                from_version=backup_from_version,
                digest=backup_sha256,
            )
            try:
                backup_bytes = _read_regular_bytes(
                    backup,
                    kind="operator config backup",
                )
            except ConfigSchemaError as exc:
                raise ConfigBackupIntegrityError(str(exc)) from exc
            if _sha256_bytes(backup_bytes) != backup_sha256:
                raise ConfigBackupIntegrityError(
                    "operator config backup digest does not match requested rollback identity"
                )
            backup_payload = _decode_config(backup_bytes)
            if backup_payload["schema_version"] != backup_from_version:
                raise ConfigBackupIntegrityError(
                    "operator config backup schema does not match requested rollback version"
                )
            self._validate(backup_payload)
            _atomic_publish(
                destination,
                backup_bytes,
                expected_sha256=backup_sha256,
            )
            return ConfigMigrationJournal(
                status="RESTORED",
                from_version=current_payload["schema_version"],
                to_version=backup_from_version,
                before_sha256=current_sha256,
                after_sha256=backup_sha256,
                backup_sha256=backup_sha256,
            )
