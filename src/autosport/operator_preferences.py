from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path, PureWindowsPath
from typing import Any

from .dataset import ReplayDataset, load_dataset
from .integrity import atomic_write_json, durable_path_lock
from .research_strategy import ResearchStrategyPlan
from .strategies import strategy_spec, validate_strategy_configuration


_SCHEMA_VERSION = 1
_OUTER_KEYS = frozenset({"schema_version", "preferences"})
_PREFERENCE_KEYS = frozenset(
    {
        "strategy_id",
        "remembered_dataset_path",
        "remembered_research_plan_path",
        "replay_speed",
        "live_mode",
    }
)


class OperatorPreferencesError(ValueError):
    """Operator-preference bytes are unavailable, malformed, or unsafe to use."""


class ReplaySpeed(str, Enum):
    EVENT_DRIVEN = "event_driven"
    REALTIME = "realtime"
    X10 = "10x"
    X100 = "100x"
    X1000 = "1000x"


class LiveMode(str, Enum):
    PUBLIC_PREVIEW = "public_preview"
    API_KEY = "api_key"


def _require_plain_text(value: object, *, field: str, max_length: int) -> str:
    if type(value) is not str:
        raise OperatorPreferencesError(f"{field} must be a string")
    if not value or value.strip() != value:
        raise OperatorPreferencesError(f"{field} must be non-empty without surrounding whitespace")
    if len(value) > max_length:
        raise OperatorPreferencesError(f"{field} is too long")
    if any(ord(character) < 32 for character in value):
        raise OperatorPreferencesError(f"{field} must not contain control characters")
    return value


def _is_absolute_remembered_path(value: str) -> bool:
    # CI also runs on non-Windows hosts, so recognize Windows absolute/UNC paths
    # explicitly rather than making persisted Windows identity depend on host OS.
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _validate_remembered_path(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    text = _require_plain_text(value, field=field, max_length=32767)
    if not _is_absolute_remembered_path(text):
        raise OperatorPreferencesError(
            f"{field} must be absolute so remembered selection does not depend on CWD"
        )
    return text


def _reject_json_constant(value: str) -> None:
    raise OperatorPreferencesError(f"non-finite JSON number is not allowed: {value}")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OperatorPreferencesError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_loads(text: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except OperatorPreferencesError:
        raise
    except json.JSONDecodeError as exc:
        raise OperatorPreferencesError("operator preferences must be valid JSON") from exc


@dataclass(frozen=True, slots=True)
class OperatorPreferences:
    """Remembered UI selections only; this object carries no domain authority.

    Paths are remembered choices. A consumer must freshly validate/open the
    dataset or research plan before use. LiveMode.API_KEY records only the
    selected operating mode and never stores credentials.
    """

    strategy_id: str = "baseline-v1"
    remembered_dataset_path: str | None = None
    remembered_research_plan_path: str | None = None
    replay_speed: ReplaySpeed = ReplaySpeed.EVENT_DRIVEN
    live_mode: LiveMode = LiveMode.PUBLIC_PREVIEW

    def __post_init__(self) -> None:
        _require_plain_text(self.strategy_id, field="strategy_id", max_length=128)
        _validate_remembered_path(
            self.remembered_dataset_path,
            field="remembered_dataset_path",
        )
        _validate_remembered_path(
            self.remembered_research_plan_path,
            field="remembered_research_plan_path",
        )
        if type(self.replay_speed) is not ReplaySpeed:
            raise OperatorPreferencesError("replay_speed must be a ReplaySpeed")
        if type(self.live_mode) is not LiveMode:
            raise OperatorPreferencesError("live_mode must be a LiveMode")

    @property
    def remembered_paths_are_validated(self) -> bool:
        """Persisted path selection never proves current content validity."""
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "preferences": {
                "strategy_id": self.strategy_id,
                "remembered_dataset_path": self.remembered_dataset_path,
                "remembered_research_plan_path": self.remembered_research_plan_path,
                "replay_speed": self.replay_speed.value,
                "live_mode": self.live_mode.value,
            },
        }

    @classmethod
    def from_payload(cls, payload: object) -> "OperatorPreferences":
        if type(payload) is not dict:
            raise OperatorPreferencesError("operator preferences root must be an object")
        unknown_outer = set(payload) - _OUTER_KEYS
        missing_outer = _OUTER_KEYS - set(payload)
        if unknown_outer or missing_outer:
            raise OperatorPreferencesError(
                "operator preferences root keys do not match schema"
            )
        if type(payload["schema_version"]) is not int or payload["schema_version"] != _SCHEMA_VERSION:
            raise OperatorPreferencesError(
                f"unsupported operator preferences schema_version: {payload['schema_version']!r}"
            )

        raw = payload["preferences"]
        if type(raw) is not dict:
            raise OperatorPreferencesError("preferences must be an object")
        unknown = set(raw) - _PREFERENCE_KEYS
        missing = _PREFERENCE_KEYS - set(raw)
        if unknown or missing:
            raise OperatorPreferencesError("preferences keys do not match schema")

        strategy_id = _require_plain_text(
            raw["strategy_id"],
            field="strategy_id",
            max_length=128,
        )
        dataset_path = _validate_remembered_path(
            raw["remembered_dataset_path"],
            field="remembered_dataset_path",
        )
        research_plan_path = _validate_remembered_path(
            raw["remembered_research_plan_path"],
            field="remembered_research_plan_path",
        )

        replay_speed_raw = raw["replay_speed"]
        if type(replay_speed_raw) is not str:
            raise OperatorPreferencesError("replay_speed must be a string")
        try:
            replay_speed = ReplaySpeed(replay_speed_raw)
        except ValueError as exc:
            raise OperatorPreferencesError(
                f"unsupported replay_speed: {replay_speed_raw!r}"
            ) from exc

        live_mode_raw = raw["live_mode"]
        if type(live_mode_raw) is not str:
            raise OperatorPreferencesError("live_mode must be a string")
        try:
            live_mode = LiveMode(live_mode_raw)
        except ValueError as exc:
            raise OperatorPreferencesError(
                f"unsupported live_mode: {live_mode_raw!r}"
            ) from exc

        return cls(
            strategy_id=strategy_id,
            remembered_dataset_path=dataset_path,
            remembered_research_plan_path=research_plan_path,
            replay_speed=replay_speed,
            live_mode=live_mode,
        )



@dataclass(frozen=True, slots=True)
class ValidatedOperatorPreferences:
    """Freshly revalidated selections ready for a current-process consumer."""

    preferences: OperatorPreferences
    dataset: ReplayDataset | None
    research_plan: ResearchStrategyPlan | None

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False


def validate_preferences_for_use(
    preferences: OperatorPreferences,
) -> ValidatedOperatorPreferences:
    """Re-resolve remembered selections through canonical current validators.

    Persisted preferences are never sufficient authority for a dataset, research
    plan, or strategy.  Every process that wants to use them must call the
    canonical loaders against the bytes currently present on disk.
    """

    if type(preferences) is not OperatorPreferences:
        raise OperatorPreferencesError(
            "preferences must be an exact OperatorPreferences instance"
        )

    spec = strategy_spec(preferences.strategy_id)
    research_plan: ResearchStrategyPlan | None = None
    if spec.requires_research_plan:
        if preferences.remembered_research_plan_path is None:
            raise OperatorPreferencesError(
                "selected strategy requires a remembered research-plan path"
            )
        research_plan = ResearchStrategyPlan.from_path(
            preferences.remembered_research_plan_path
        )

    validate_strategy_configuration(preferences.strategy_id, research_plan)

    dataset = (
        load_dataset(preferences.remembered_dataset_path)
        if preferences.remembered_dataset_path is not None
        else None
    )

    return ValidatedOperatorPreferences(
        preferences=preferences,
        dataset=dataset,
        research_plan=research_plan,
    )

def operator_preferences_path(workspace: str | Path) -> Path:
    destination = Path(workspace)
    if not destination.is_absolute():
        raise OperatorPreferencesError(
            "workspace must be absolute so operator-preference identity does not depend on CWD"
        )
    return destination / "operator-preferences.json"


class OperatorPreferencesStore:
    """Durable-path-serialized whole-file store for non-secret remembered operator choices."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise OperatorPreferencesError(
                "operator preferences path must be absolute so identity does not depend on CWD"
            )

    def load(self) -> OperatorPreferences:
        with durable_path_lock(self.path):
            if not self.path.exists():
                return OperatorPreferences()
            try:
                raw = self.path.read_bytes()
            except OSError as exc:
                raise OperatorPreferencesError(
                    "operator preferences could not be read"
                ) from exc
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise OperatorPreferencesError(
                    "operator preferences must be UTF-8 JSON"
                ) from exc
            return OperatorPreferences.from_payload(_strict_json_loads(text))

    def save(self, preferences: OperatorPreferences) -> OperatorPreferences:
        if type(preferences) is not OperatorPreferences:
            raise OperatorPreferencesError(
                "preferences must be an exact OperatorPreferences instance"
            )
        payload = preferences.to_payload()
        with durable_path_lock(self.path):
            try:
                atomic_write_json(self.path, payload)
                raw = self.path.read_bytes()
            except OSError as exc:
                raise OperatorPreferencesError(
                    "operator preferences could not be durably published"
                ) from exc
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise OperatorPreferencesError(
                    "published operator preferences are not UTF-8"
                ) from exc
            published = OperatorPreferences.from_payload(_strict_json_loads(text))
            if published != preferences:
                raise OperatorPreferencesError(
                    "published operator preferences do not match intended preferences"
                )
            return published
