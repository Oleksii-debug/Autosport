from __future__ import annotations

import pytest

import scripts.cancel_superseded_pr_workflow_runs as controller_module
from scripts.cancel_superseded_pr_workflow_runs import (
    CancellationError,
    CancellationResult,
    PullRequestQualification,
    WorkflowRun,
    _write_github_output,
    admit_current_head,
    cancel_superseded,
    select_superseded_runs,
)


HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40


def _run(
    run_id: int,
    head_sha: str,
    *,
    workflow_name: str = "CI",
    pr_numbers: tuple[int, ...] = (2008,),
) -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        head_sha=head_sha,
        workflow_name=workflow_name,
        pr_numbers=pr_numbers,
        status="in_progress",
    )


def test_selects_only_superseded_runs_for_same_pr_and_workflow() -> None:
    runs = (
        _run(10, HEAD_A),
        _run(11, HEAD_B),
        _run(12, HEAD_A, workflow_name="Windows candidate"),
        _run(13, HEAD_A, pr_numbers=(999,)),
    )
    assert select_superseded_runs(
        runs,
        pr_number=2008,
        live_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=11,
    ) == (10,)


@pytest.mark.parametrize(
    "pr_numbers",
    [
        (2008, 999),
        (999, 2008),
        (2008, 2008),
    ],
)
def test_multi_reference_run_never_grants_single_pr_cancellation_authority(
    pr_numbers: tuple[int, ...],
) -> None:
    assert select_superseded_runs(
        (_run(14, HEAD_A, pr_numbers=pr_numbers),),
        pr_number=2008,
        live_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=11,
    ) == ()


def test_current_run_is_never_selected_even_if_payload_is_inconsistent() -> None:
    assert select_superseded_runs(
        (_run(21, HEAD_A),),
        pr_number=2008,
        live_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=21,
    ) == ()


def test_delayed_stale_run_is_selected_even_with_later_run_id() -> None:
    assert select_superseded_runs(
        (
            _run(30, HEAD_A),
            _run(31, HEAD_B),
            _run(32, HEAD_C),
        ),
        pr_number=2008,
        live_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=31,
    ) == (30, 32)


class FakeApi:
    def __init__(self, heads: list[str], runs: tuple[WorkflowRun, ...]) -> None:
        self._heads = list(heads)
        self._runs = runs
        self.active_calls = 0
        self.cancelled: list[int] = []

    def live_pr_head(self, pr_number: int) -> str:
        assert pr_number == 2008
        if not self._heads:
            raise AssertionError("unexpected live-head read")
        return self._heads.pop(0)

    def active_runs(self) -> tuple[WorkflowRun, ...]:
        self.active_calls += 1
        return self._runs

    def cancel(self, run_id: int) -> None:
        self.cancelled.append(run_id)


def test_read_only_admission_distinguishes_current_and_stale_heads() -> None:
    current_api = FakeApi([HEAD_B, HEAD_B], ())
    assert admit_current_head(
        api=current_api,
        pr_number=2008,
        event_head_sha=HEAD_B,
    ) == CancellationResult(current_head=True, cancelled_run_ids=())

    stale_api = FakeApi([HEAD_B], ())
    assert admit_current_head(
        api=stale_api,
        pr_number=2008,
        event_head_sha=HEAD_A,
    ) == CancellationResult(current_head=False, cancelled_run_ids=())
    assert current_api.active_calls == stale_api.active_calls == 0


def test_stale_rerun_has_zero_cancellation_authority() -> None:
    api = FakeApi([HEAD_B], (_run(30, HEAD_B),))
    result = cancel_superseded(
        api=api,
        pr_number=2008,
        event_head_sha=HEAD_A,
        workflow_name="CI",
        current_run_id=29,
    )
    assert result == CancellationResult(current_head=False, cancelled_run_ids=())
    assert api.active_calls == 0
    assert api.cancelled == []


def test_current_head_cancels_stale_runs_on_both_sides_of_run_id_ordering() -> None:
    api = FakeApi(
        [HEAD_B, HEAD_B, HEAD_B, HEAD_B],
        (
            _run(40, HEAD_A),
            _run(41, HEAD_B),
            _run(42, HEAD_C),
        ),
    )
    result = cancel_superseded(
        api=api,
        pr_number=2008,
        event_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=41,
    )
    assert result == CancellationResult(current_head=True, cancelled_run_ids=(40, 42))
    assert api.cancelled == [40, 42]


def test_qualification_eq_rebind_cannot_self_confirm_head_change() -> None:
    api = FakeApi(
        [HEAD_B, HEAD_C],
        (
            _run(50, HEAD_A),
            _run(51, HEAD_C),
        ),
    )
    original = PullRequestQualification.__eq__

    try:
        PullRequestQualification.__eq__ = lambda _self, _other: True
        result = cancel_superseded(
            api=api,
            pr_number=2008,
            event_head_sha=HEAD_B,
            workflow_name="CI",
            current_run_id=49,
        )
    finally:
        PullRequestQualification.__eq__ = original

    assert result == CancellationResult(current_head=False, cancelled_run_ids=())
    assert api.cancelled == []


def test_qualification_field_class_shadow_is_rejected_before_read() -> None:
    qualification = PullRequestQualification(
        head_sha=HEAD_B,
        integration_capable=True,
    )

    class QualificationApi:
        def live_pr_qualification(self, pr_number: int):
            assert pr_number == 2008
            return qualification

    original_present = "head_sha" in PullRequestQualification.__dict__
    assert original_present is False
    hostile_called = False

    def forged_head(_self):
        nonlocal hostile_called
        hostile_called = True
        return HEAD_B

    try:
        PullRequestQualification.head_sha = property(forged_head)
        with pytest.raises(
            CancellationError,
            match="pull request qualification authority changed",
        ):
            admit_current_head(
                api=QualificationApi(),
                pr_number=2008,
                event_head_sha=HEAD_B,
            )
    finally:
        delattr(PullRequestQualification, "head_sha")

    assert hostile_called is False


def test_qualification_constructor_code_mutation_is_rejected() -> None:
    qualification = PullRequestQualification(
        head_sha=HEAD_B,
        integration_capable=True,
    )

    class QualificationApi:
        def live_pr_qualification(self, pr_number: int):
            assert pr_number == 2008
            return qualification

    target = PullRequestQualification.__init__
    original_code = target.__code__

    def forged_init(self, head_sha, integration_capable):
        del self, head_sha, integration_capable

    forged_code = forged_init.__code__.replace(
        co_freevars=original_code.co_freevars,
    )
    try:
        target.__code__ = forged_code
        with pytest.raises(
            CancellationError,
            match="pull request qualification authority changed",
        ):
            admit_current_head(
                api=QualificationApi(),
                pr_number=2008,
                event_head_sha=HEAD_B,
            )
    finally:
        target.__code__ = original_code


def test_coordinated_qualification_root_rebind_cannot_forge_trusted_head(
    monkeypatch,
) -> None:
    api = controller_module.GitHubApi(
        repository="owner/repo",
        token="token",
    )
    api._pull_request = lambda pr_number: {
        "state": "open",
        "draft": False,
        "head": {
            "sha": HEAD_B,
            "repo": {"full_name": "owner/repo"},
        },
        "base": {"repo": {"full_name": "owner/repo"}},
    } if pr_number == 2008 else (_ for _ in ()).throw(AssertionError(pr_number))

    class ForgedQualification:
        def __init__(self, head_sha: str, integration_capable: bool) -> None:
            del head_sha, integration_capable
            self.head_sha = HEAD_A
            self.integration_capable = True

    forged_init = ForgedQualification.__dict__["__init__"]
    monkeypatch.setattr(
        controller_module,
        "PullRequestQualification",
        ForgedQualification,
    )
    # Recreate the old coordinated witness-global attack even though these names
    # are no longer authority-bearing. A secure reader must ignore all of them.
    monkeypatch.setattr(
        controller_module,
        "_PULL_REQUEST_QUALIFICATION_TYPE",
        ForgedQualification,
        raising=False,
    )
    monkeypatch.setattr(
        controller_module,
        "_PULL_REQUEST_QUALIFICATION_DICT_DESCRIPTOR",
        ForgedQualification.__dict__["__dict__"],
        raising=False,
    )
    monkeypatch.setattr(
        controller_module,
        "_PULL_REQUEST_QUALIFICATION_INIT",
        forged_init,
        raising=False,
    )
    monkeypatch.setattr(
        controller_module,
        "_PULL_REQUEST_QUALIFICATION_INIT_CODE",
        forged_init.__code__,
        raising=False,
    )
    monkeypatch.setattr(
        controller_module,
        "_PULL_REQUEST_QUALIFICATION_FIELD_CLASS_WITNESSES",
        tuple(
            (
                name,
                name in ForgedQualification.__dict__,
                ForgedQualification.__dict__.get(name),
            )
            for name in ("head_sha", "integration_capable")
        ),
        raising=False,
    )

    assert api.live_pr_qualification(2008) == (HEAD_B, True)
    assert admit_current_head(
        api=api,
        pr_number=2008,
        event_head_sha=HEAD_A,
    ) == CancellationResult(current_head=False, cancelled_run_ids=())
    assert admit_current_head(
        api=api,
        pr_number=2008,
        event_head_sha=HEAD_B,
    ) == CancellationResult(current_head=True, cancelled_run_ids=())


def test_pull_request_target_does_not_trust_rebound_positive_int_helper(
    monkeypatch,
) -> None:
    api = controller_module.GitHubApi(
        repository="owner/repo",
        token="token",
    )
    requested: list[str] = []

    def fake_request(path: str, **_kwargs):
        requested.append(path)
        return {
            "state": "open",
            "draft": False,
            "head": {
                "sha": HEAD_B,
                "repo": {"full_name": "owner/repo"},
            },
            "base": {"repo": {"full_name": "owner/repo"}},
        }

    def forged_positive_int(_value, *, field: str) -> int:
        del field
        return 999

    monkeypatch.setattr(api, "_request", fake_request)
    monkeypatch.setattr(
        controller_module,
        "_require_positive_int",
        forged_positive_int,
    )

    assert api.live_pr_qualification(2008) == (HEAD_B, True)
    assert requested == ["/pulls/2008"]


def test_live_pr_qualification_does_not_trust_rebound_sha_helper(
    monkeypatch,
) -> None:
    api = controller_module.GitHubApi(
        repository="owner/repo",
        token="token",
    )
    api._pull_request = lambda pr_number: {
        "state": "open",
        "draft": False,
        "head": {
            "sha": HEAD_B,
            "repo": {"full_name": "owner/repo"},
        },
        "base": {"repo": {"full_name": "owner/repo"}},
    } if pr_number == 2008 else (_ for _ in ()).throw(AssertionError(pr_number))

    def forged_sha(_value, *, field: str) -> str:
        del field
        return HEAD_A

    monkeypatch.setattr(controller_module, "_require_sha", forged_sha)

    assert api.live_pr_qualification(2008) == (HEAD_B, True)
    assert admit_current_head(
        api=api,
        pr_number=2008,
        event_head_sha=HEAD_A,
    ) == CancellationResult(current_head=False, cancelled_run_ids=())


def test_in_place_qualification_state_drift_revokes_cancellation() -> None:
    qualification = PullRequestQualification(
        head_sha=HEAD_B,
        integration_capable=True,
    )

    class MutableQualificationApi:
        def __init__(self) -> None:
            self.reads = 0
            self.cancelled: list[int] = []

        def live_pr_qualification(self, pr_number: int):
            assert pr_number == 2008
            self.reads += 1
            if self.reads == 2:
                object.__setattr__(qualification, "head_sha", HEAD_C)
            return qualification

        def active_runs(self):
            return (_run(50, HEAD_A),)

        def cancel(self, run_id: int) -> None:
            self.cancelled.append(run_id)

    api = MutableQualificationApi()
    result = cancel_superseded(
        api=api,
        pr_number=2008,
        event_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=49,
    )

    assert result == CancellationResult(current_head=False, cancelled_run_ids=())
    assert api.cancelled == []


def test_head_change_after_run_listing_revokes_cancellation_authority() -> None:
    api = FakeApi(
        [HEAD_B, HEAD_C],
        (
            _run(50, HEAD_A),
            _run(51, HEAD_C),
        ),
    )
    result = cancel_superseded(
        api=api,
        pr_number=2008,
        event_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=49,
    )
    assert result == CancellationResult(current_head=False, cancelled_run_ids=())
    assert api.active_calls == 1
    assert api.cancelled == []


def test_aba_head_change_immediately_before_cancel_revokes_authority() -> None:
    api = FakeApi(
        [HEAD_B, HEAD_B, HEAD_A],
        (
            _run(60, HEAD_A),
            _run(61, HEAD_B),
        ),
    )
    result = cancel_superseded(
        api=api,
        pr_number=2008,
        event_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=61,
    )
    assert result == CancellationResult(current_head=False, cancelled_run_ids=())
    assert api.active_calls == 1
    assert api.cancelled == []


def test_head_change_between_multiple_cancellations_stops_remaining_posts() -> None:
    api = FakeApi(
        [HEAD_B, HEAD_B, HEAD_B, HEAD_C],
        (
            _run(70, HEAD_A),
            _run(71, HEAD_A),
            _run(72, HEAD_B),
        ),
    )
    result = cancel_superseded(
        api=api,
        pr_number=2008,
        event_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=72,
    )
    assert result == CancellationResult(current_head=False, cancelled_run_ids=(70,))
    assert api.cancelled == [70]


def test_github_output_exposes_only_boolean_current_head(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "github-output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    _write_github_output(CancellationResult(current_head=True, cancelled_run_ids=(7, 9)))
    assert output_path.read_text(encoding="utf-8") == "current_head=true\n"


def test_invalid_sha_fails_closed() -> None:
    with pytest.raises(CancellationError):
        select_superseded_runs(
            (),
            pr_number=2008,
            live_head_sha="not-a-sha",
            workflow_name="CI",
            current_run_id=1,
        )

def test_parse_run_ignores_rebound_coordinate_validators(monkeypatch) -> None:
    monkeypatch.setattr(
        controller_module,
        "_require_positive_int",
        lambda _value, *, field: 999,
    )
    monkeypatch.setattr(
        controller_module,
        "_require_sha",
        lambda _value, *, field: HEAD_C,
    )
    monkeypatch.setattr(
        controller_module,
        "_ACTIVE_STATUSES",
        ("attacker",),
    )

    run = controller_module.parse_run(
        {
            "id": 77,
            "head_sha": HEAD_A,
            "name": "CI",
            "status": "queued",
            "pull_requests": [{"number": 2008}],
        }
    )

    assert run == WorkflowRun(
        run_id=77,
        head_sha=HEAD_A,
        workflow_name="CI",
        pr_numbers=(2008,),
        status="queued",
    )


def test_parse_run_rejects_inplace_workflow_run_constructor_mutation(
    monkeypatch,
) -> None:
    workflow_run_init = WorkflowRun.__dict__["__init__"]

    def forged_init(
        self,
        run_id,
        head_sha,
        workflow_name,
        pr_numbers,
        status,
    ) -> None:
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "head_sha", head_sha)
        object.__setattr__(self, "workflow_name", workflow_name)
        object.__setattr__(self, "pr_numbers", (999,))
        object.__setattr__(self, "status", status)

    forged_code = forged_init.__code__.replace(
        co_freevars=workflow_run_init.__code__.co_freevars,
    )
    monkeypatch.setattr(workflow_run_init, "__code__", forged_code)

    with pytest.raises(
        CancellationError,
        match="workflow run parser authority changed",
    ):
        controller_module.parse_run(
            {
                "id": 77,
                "head_sha": HEAD_A,
                "name": "CI",
                "status": "queued",
                "pull_requests": [{"number": 2008}],
            }
        )


def test_commit_association_page_bound_cannot_hide_second_pr(monkeypatch) -> None:
    api = controller_module.GitHubApi(
        repository="owner/repo",
        token="token",
    )
    requested: list[str] = []
    page_one = [
        {"number": 2008, "head": {"sha": HEAD_A}}
        for _ in range(100)
    ]
    page_two = [{"number": 2009, "head": {"sha": HEAD_A}}]

    def fake_request(path: str, **_kwargs):
        requested.append(path)
        if "page=1" in path:
            return page_one
        if "page=2" in path:
            return page_two
        raise AssertionError(path)

    monkeypatch.setattr(api, "_request", fake_request)
    monkeypatch.setattr(controller_module, "_PULLS_PER_PAGE", 101)
    monkeypatch.setattr(
        controller_module,
        "urlencode",
        lambda _params: "per_page=999&page=1",
    )

    with pytest.raises(
        CancellationError,
        match="exactly one associated pull request",
    ):
        api.associated_pr_number(HEAD_A)

    assert requested == [
        f"/commits/{HEAD_A}/pulls?per_page=100&page=1",
        f"/commits/{HEAD_A}/pulls?per_page=100&page=2",
    ]


def test_base_active_run_page_bound_and_parser_are_frozen(monkeypatch) -> None:
    api = controller_module.GitHubApi(
        repository="owner/repo",
        token="token",
    )
    requested: list[str] = []

    def run_payload(run_id: int) -> dict[str, object]:
        return {
            "id": run_id,
            "head_sha": HEAD_A,
            "name": "CI",
            "status": "queued",
            "pull_requests": [{"number": 2008}],
        }

    def fake_request(path: str, **_kwargs):
        requested.append(path)
        if "page=1" in path:
            return {
                "total_count": 101,
                "workflow_runs": [run_payload(run_id) for run_id in range(1, 101)],
            }
        if "page=2" in path:
            return {
                "total_count": 101,
                "workflow_runs": [run_payload(101)],
            }
        raise AssertionError(path)

    monkeypatch.setattr(api, "_request", fake_request)
    monkeypatch.setattr(controller_module, "_RUNS_PER_PAGE", 101)
    monkeypatch.setattr(
        controller_module,
        "urlencode",
        lambda _params: "event=attacker&per_page=999&page=1",
    )
    monkeypatch.setattr(
        controller_module,
        "parse_run",
        lambda _payload: (_ for _ in ()).throw(
            AssertionError("rebound parser must not execute")
        ),
    )

    runs = api._active_runs_for_status("queued")

    assert tuple(run.run_id for run in runs) == tuple(range(1, 102))
    assert requested == [
        "/actions/runs?event=pull_request&status=queued&per_page=100&page=1",
        "/actions/runs?event=pull_request&status=queued&per_page=100&page=2",
    ]

def test_selector_ignores_rebound_coordinate_validators(monkeypatch) -> None:
    monkeypatch.setattr(
        controller_module,
        "_require_positive_int",
        lambda _value, *, field: 999,
    )
    monkeypatch.setattr(
        controller_module,
        "_require_sha",
        lambda _value, *, field: HEAD_A,
    )

    assert select_superseded_runs(
        (
            _run(10, HEAD_A),
            _run(11, HEAD_B),
        ),
        pr_number=2008,
        live_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=11,
    ) == (10,)


def test_cancel_rejects_preentry_qualification_snapshot_rebind(monkeypatch) -> None:
    forged_calls: list[int] = []

    def forged_snapshot(api, pr_number: int, **_kwargs) -> tuple[str, bool]:
        del api
        forged_calls.append(pr_number)
        return HEAD_B, True

    monkeypatch.setattr(
        controller_module,
        "_qualification_snapshot",
        forged_snapshot,
    )
    api = FakeApi([HEAD_B], (_run(10, HEAD_A),))

    with pytest.raises(
        CancellationError,
        match="superseded-run cancellation authority changed",
    ):
        cancel_superseded(
            api=api,
            pr_number=2008,
            event_head_sha=HEAD_B,
            workflow_name="CI",
            current_run_id=11,
        )

    assert forged_calls == []
    assert api.active_calls == 0
    assert api.cancelled == []


def test_cancel_rejects_preentry_selector_rebind(monkeypatch) -> None:
    forged_calls: list[str] = []

    def forged_selector(*_args, **_kwargs) -> tuple[int, ...]:
        forged_calls.append("selector")
        return (999,)

    monkeypatch.setattr(
        controller_module,
        "select_superseded_runs",
        forged_selector,
    )
    api = FakeApi([HEAD_B], (_run(10, HEAD_A),))

    with pytest.raises(
        CancellationError,
        match="superseded-run cancellation authority changed",
    ):
        cancel_superseded(
            api=api,
            pr_number=2008,
            event_head_sha=HEAD_B,
            workflow_name="CI",
            current_run_id=11,
        )

    assert forged_calls == []
    assert api.active_calls == 0
    assert api.cancelled == []


def test_cancel_rejects_nested_snapshot_kwdefault_rebase(monkeypatch) -> None:
    defaults = controller_module._qualification_snapshot.__kwdefaults__
    assert defaults is not None

    def forged_reader(_qualification: object) -> tuple[str, bool]:
        return HEAD_B, True

    monkeypatch.setitem(
        defaults,
        "_qualification_state_reader",
        forged_reader,
    )
    api = FakeApi([HEAD_B], (_run(10, HEAD_A),))

    with pytest.raises(
        CancellationError,
        match="superseded-run cancellation authority changed",
    ):
        cancel_superseded(
            api=api,
            pr_number=2008,
            event_head_sha=HEAD_B,
            workflow_name="CI",
            current_run_id=11,
        )

    assert api.active_calls == 0
    assert api.cancelled == []


def test_cancel_rejects_inflight_active_runs_shadow(monkeypatch) -> None:
    forged_calls: list[str] = []

    def forged_active_runs() -> tuple[WorkflowRun, ...]:
        forged_calls.append("active")
        return (_run(999, HEAD_A),)

    class ShadowingApi(FakeApi):
        def live_pr_head(self, pr_number: int) -> str:
            head = super().live_pr_head(pr_number)
            monkeypatch.setattr(self, "active_runs", forged_active_runs)
            return head

    api = ShadowingApi([HEAD_B], (_run(10, HEAD_A),))

    with pytest.raises(
        CancellationError,
        match="superseded-run cancellation authority changed",
    ):
        cancel_superseded(
            api=api,
            pr_number=2008,
            event_head_sha=HEAD_B,
            workflow_name="CI",
            current_run_id=11,
        )

    assert forged_calls == []
    assert api.cancelled == []

