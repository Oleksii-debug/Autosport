from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest

import autosport.operator_preferences as preferences_module
from autosport.operator_preferences import (
    LiveMode,
    OperatorPreferences,
    OperatorPreferencesError,
    OperatorPreferencesStore,
    ReplaySpeed,
    operator_preferences_path,
    validate_preferences_for_use,
)


def test_missing_store_returns_defaults_without_creating_file(tmp_path: Path) -> None:
    path = tmp_path / "operator-preferences.json"
    store = OperatorPreferencesStore(path)

    loaded = store.load()

    assert loaded == OperatorPreferences()
    assert not path.exists()
    assert loaded.remembered_paths_are_validated is False
    assert loaded.execution_authorized is False
    assert loaded.real_money_execution is False


def test_round_trip_preserves_unicode_spaces_and_non_secret_modes(tmp_path: Path) -> None:
    path = tmp_path / "operator-preferences.json"
    dataset = tmp_path / "Дані сезону" / "матчі.jsonl"
    plan = tmp_path / "Плани" / "основний план.json"
    expected = OperatorPreferences(
        strategy_id="baseline-v1",
        remembered_dataset_path=str(dataset),
        remembered_research_plan_path=str(plan),
        replay_speed=ReplaySpeed.X100,
        live_mode=LiveMode.API_KEY,
    )

    saved = OperatorPreferencesStore(path).save(expected)
    reopened = OperatorPreferencesStore(path).load()

    assert saved == expected
    assert reopened == expected
    assert reopened.remembered_paths_are_validated is False
    text = path.read_text(encoding="utf-8")
    assert "Дані сезону" in text
    assert "api_key" in text


def test_windows_absolute_paths_are_host_independent() -> None:
    value = OperatorPreferences(
        remembered_dataset_path=r"C:\Users\Олексій\Autosport data\matches.jsonl",
        remembered_research_plan_path=r"\\server\share\Плани\plan.json",
    )

    assert value.remembered_dataset_path == r"C:\Users\Олексій\Autosport data\matches.jsonl"
    assert value.remembered_research_plan_path == r"\\server\share\Плани\plan.json"


@pytest.mark.parametrize(
    "field,value",
    [
        ("remembered_dataset_path", "relative/data.jsonl"),
        ("remembered_research_plan_path", "plans/plan.json"),
    ],
)
def test_relative_remembered_paths_fail_closed(field: str, value: str) -> None:
    kwargs = {field: value}
    with pytest.raises(OperatorPreferencesError, match="must be absolute"):
        OperatorPreferences(**kwargs)


def test_store_path_must_be_absolute() -> None:
    with pytest.raises(OperatorPreferencesError, match="path must be absolute"):
        OperatorPreferencesStore("relative/operator-preferences.json")


def test_workspace_path_helper_is_cwd_independent(tmp_path: Path) -> None:
    assert operator_preferences_path(tmp_path) == tmp_path / "operator-preferences.json"
    with pytest.raises(OperatorPreferencesError, match="workspace must be absolute"):
        operator_preferences_path("relative-workspace")


def test_duplicate_json_keys_are_rejected_without_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "operator-preferences.json"
    raw = (
        '{"schema_version":1,"schema_version":1,"preferences":'
        '{"strategy_id":"baseline-v1","remembered_dataset_path":null,'
        '"remembered_research_plan_path":null,"replay_speed":"event_driven",'
        '"live_mode":"public_preview"}}'
    )
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(OperatorPreferencesError, match="duplicate JSON key"):
        OperatorPreferencesStore(path).load()

    assert path.read_text(encoding="utf-8") == raw


def test_unknown_secret_shaped_field_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "operator-preferences.json"
    payload = OperatorPreferences().to_payload()
    payload["preferences"]["api_token"] = "must-never-be-accepted"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(OperatorPreferencesError, match="keys do not match schema"):
        OperatorPreferencesStore(path).load()


@pytest.mark.parametrize("bad_version", [True, 0, 2, "1"])
def test_schema_version_is_exact_integer_one(tmp_path: Path, bad_version: object) -> None:
    path = tmp_path / "operator-preferences.json"
    payload = OperatorPreferences().to_payload()
    payload["schema_version"] = bad_version
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(OperatorPreferencesError, match="schema_version"):
        OperatorPreferencesStore(path).load()


@pytest.mark.parametrize(
    "field,value",
    [
        ("replay_speed", True),
        ("replay_speed", "fastest"),
        ("live_mode", 1),
        ("live_mode", "secret_token"),
    ],
)
def test_mode_fields_do_not_coerce(field: str, value: object, tmp_path: Path) -> None:
    path = tmp_path / "operator-preferences.json"
    payload = OperatorPreferences().to_payload()
    payload["preferences"][field] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(OperatorPreferencesError):
        OperatorPreferencesStore(path).load()


def test_invalid_json_is_reported_without_self_healing(tmp_path: Path) -> None:
    path = tmp_path / "operator-preferences.json"
    raw = b'{"schema_version": 1,'
    path.write_bytes(raw)

    with pytest.raises(OperatorPreferencesError, match="valid JSON"):
        OperatorPreferencesStore(path).load()

    assert path.read_bytes() == raw


def test_invalid_utf8_is_reported_without_self_healing(tmp_path: Path) -> None:
    path = tmp_path / "operator-preferences.json"
    raw = b"\xff\xfe\x00"
    path.write_bytes(raw)

    with pytest.raises(OperatorPreferencesError, match="UTF-8"):
        OperatorPreferencesStore(path).load()

    assert path.read_bytes() == raw


def test_save_fails_if_published_readback_differs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "operator-preferences.json"
    intended = OperatorPreferences(strategy_id="baseline-v1")
    divergent = OperatorPreferences(strategy_id="other-strategy").to_payload()

    def publish_different(destination: str | Path, payload: dict[str, object]) -> None:
        Path(destination).write_text(
            json.dumps(divergent, ensure_ascii=False),
            encoding="utf-8",
        )

    monkeypatch.setattr(preferences_module, "atomic_write_json", publish_different)

    with pytest.raises(OperatorPreferencesError, match="do not match intended"):
        OperatorPreferencesStore(path).save(intended)


def test_restart_value_does_not_depend_on_changed_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "operator-preferences.json"
    dataset = tmp_path / "selected" / "dataset.jsonl"
    expected = OperatorPreferences(remembered_dataset_path=str(dataset))
    store = OperatorPreferencesStore(path)
    store.save(expected)

    other = tmp_path / "other-cwd"
    other.mkdir()
    monkeypatch.chdir(other)

    assert OperatorPreferencesStore(path).load() == expected


def test_concurrent_saves_publish_one_complete_valid_image(tmp_path: Path) -> None:
    path = tmp_path / "operator-preferences.json"
    store = OperatorPreferencesStore(path)
    choices = [
        OperatorPreferences(strategy_id=f"strategy-{index}", replay_speed=ReplaySpeed.X10)
        for index in range(8)
    ]
    barrier = threading.Barrier(len(choices))
    failures: list[BaseException] = []

    def writer(choice: OperatorPreferences) -> None:
        try:
            barrier.wait()
            store.save(choice)
        except BaseException as exc:  # pragma: no cover - diagnostic collection
            failures.append(exc)

    threads = [threading.Thread(target=writer, args=(choice,)) for choice in choices]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    assert store.load() in choices

def test_validate_for_use_reloads_dataset_through_canonical_loader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "dataset"
    inactive_plan_path = tmp_path / "old-plan.json"
    preferences = OperatorPreferences(
        remembered_dataset_path=str(dataset_path),
        remembered_research_plan_path=str(inactive_plan_path),
    )
    dataset = object()
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        preferences_module,
        "load_dataset",
        lambda path: calls.append(("dataset", path)) or dataset,
    )
    monkeypatch.setattr(
        preferences_module,
        "strategy_spec",
        lambda strategy_id: calls.append(("spec", strategy_id))
        or type("Spec", (), {"requires_research_plan": False})(),
    )
    monkeypatch.setattr(
        preferences_module,
        "validate_strategy_configuration",
        lambda strategy_id, plan: calls.append(("validate", (strategy_id, plan))),
    )

    class _UnexpectedPlanLoader:
        @classmethod
        def from_path(cls, path: str) -> object:
            raise AssertionError(f"inactive research plan must not be loaded: {path}")

    monkeypatch.setattr(preferences_module, "ResearchStrategyPlan", _UnexpectedPlanLoader)

    resolved = validate_preferences_for_use(preferences)

    assert resolved.preferences is preferences
    assert resolved.dataset is dataset
    assert resolved.research_plan is None
    assert resolved.execution_authorized is False
    assert resolved.real_money_execution is False
    assert calls == [
        ("spec", "baseline-v1"),
        ("validate", ("baseline-v1", None)),
        ("dataset", str(dataset_path)),
    ]


def test_validate_for_use_reloads_required_research_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "plan.json"
    preferences = OperatorPreferences(
        strategy_id="research-replay-v1",
        remembered_research_plan_path=str(plan_path),
    )
    plan = object()
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        preferences_module,
        "strategy_spec",
        lambda strategy_id: calls.append(("spec", strategy_id))
        or type("Spec", (), {"requires_research_plan": True})(),
    )

    class _PlanLoader:
        @classmethod
        def from_path(cls, path: str) -> object:
            calls.append(("plan", path))
            return plan

    monkeypatch.setattr(preferences_module, "ResearchStrategyPlan", _PlanLoader)
    monkeypatch.setattr(
        preferences_module,
        "validate_strategy_configuration",
        lambda strategy_id, loaded_plan: calls.append(
            ("validate", (strategy_id, loaded_plan))
        ),
    )

    resolved = validate_preferences_for_use(preferences)

    assert resolved.dataset is None
    assert resolved.research_plan is plan
    assert calls == [
        ("spec", "research-replay-v1"),
        ("plan", str(plan_path)),
        ("validate", ("research-replay-v1", plan)),
    ]


def test_validate_for_use_rejects_missing_required_research_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preferences = OperatorPreferences(strategy_id="research-replay-v1")
    monkeypatch.setattr(
        preferences_module,
        "strategy_spec",
        lambda _strategy_id: type("Spec", (), {"requires_research_plan": True})(),
    )
    monkeypatch.setattr(
        preferences_module,
        "load_dataset",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("dataset I/O must not run before required plan validation")
        ),
    )

    with pytest.raises(OperatorPreferencesError, match="requires a remembered research-plan"):
        validate_preferences_for_use(preferences)


def test_validate_for_use_propagates_fresh_dataset_rejection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset_path = tmp_path / "dataset"
    preferences = OperatorPreferences(remembered_dataset_path=str(dataset_path))

    def reject(_path: str) -> object:
        raise ValueError("dataset digest changed")

    monkeypatch.setattr(preferences_module, "load_dataset", reject)

    with pytest.raises(ValueError, match="dataset digest changed"):
        validate_preferences_for_use(preferences)


def test_validate_for_use_rejects_subclass_authority() -> None:
    class ForgedPreferences(OperatorPreferences):
        pass

    with pytest.raises(OperatorPreferencesError, match="exact OperatorPreferences"):
        validate_preferences_for_use(ForgedPreferences())

