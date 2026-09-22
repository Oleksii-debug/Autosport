from pathlib import Path


_ACTIVITY_TYPES = (
    "types: [opened, synchronize, reopened, ready_for_review, converted_to_draft]"
)
_DRAFT_GATE = (
    "if: github.event_name != 'pull_request' "
    "|| github.event.pull_request.draft == false"
)


def _workflow(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_ci_heavy_matrix_is_deferred_only_while_pull_request_is_draft() -> None:
    workflow = _workflow(".github/workflows/ci.yml")

    assert _ACTIVITY_TYPES in workflow
    assert _DRAFT_GATE in workflow
    assert "matrix:" in workflow
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert "python-version: ['3.11', '3.12']" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "python -m pytest -v tests" in workflow
    assert "python -m autosport demo" in workflow


def test_windows_candidate_is_deferred_only_while_pull_request_is_draft() -> None:
    workflow = _workflow(".github/workflows/windows-build.yml")

    assert _ACTIVITY_TYPES in workflow
    assert _DRAFT_GATE in workflow
    assert "name: Windows candidate" in workflow
    assert "runs-on: windows-latest" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "./scripts/build_windows_candidate.ps1" in workflow
    assert "Execute packaged NVDA evidence contract" in workflow
    assert "External UIA fresh-extraction gate" in workflow


def test_endurance_matrix_is_deferred_only_while_pull_request_is_draft() -> None:
    workflow = _workflow(".github/workflows/endurance.yml")

    assert _ACTIVITY_TYPES in workflow
    assert _DRAFT_GATE in workflow
    assert "name: Endurance" in workflow
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "python -m autosport endurance" in workflow
    assert "tests/test_collector_endurance_composition.py" in workflow
    assert "Upload endurance evidence" in workflow
