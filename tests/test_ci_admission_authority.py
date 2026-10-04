from __future__ import annotations

from pathlib import Path
import pytest

import scripts.cancel_superseded_pr_workflow_runs as controller_module
from scripts.cancel_superseded_pr_workflow_runs import CancellationError, CancellationResult


HEAD_A = "a" * 40
HEAD_B = "b" * 40


def test_admission_rejects_inflight_qualification_state_reader_rebind(
    monkeypatch,
) -> None:
    forged_calls: list[object] = []

    def forged_reader(qualification: object) -> tuple[str, bool]:
        forged_calls.append(qualification)
        return HEAD_A, True

    class FakeApi:
        def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
            assert pr_number == 2008
            monkeypatch.setattr(
                controller_module,
                "_pull_request_qualification_state",
                forged_reader,
            )
            return HEAD_B, True

    with pytest.raises(
        CancellationError,
        match="pull request qualification snapshot authority changed",
    ):
        controller_module.admit_current_head(
            api=FakeApi(),
            pr_number=2008,
            event_head_sha=HEAD_A,
        )

    assert forged_calls == []


def test_admission_rejects_inflight_result_constructor_rebind(monkeypatch) -> None:
    forged_calls: list[tuple[bool, tuple[int, ...]]] = []

    class ForgedResult:
        def __init__(
            self,
            *,
            current_head: bool,
            cancelled_run_ids: tuple[int, ...],
        ) -> None:
            forged_calls.append((current_head, cancelled_run_ids))
            self.current_head = True
            self.cancelled_run_ids = ()

    class FakeApi:
        def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
            assert pr_number == 2008
            monkeypatch.setattr(
                controller_module,
                "CancellationResult",
                ForgedResult,
            )
            return HEAD_B, True

    with pytest.raises(
        CancellationError,
        match="pull request admission authority changed",
    ):
        controller_module.admit_current_head(
            api=FakeApi(),
            pr_number=2008,
            event_head_sha=HEAD_B,
        )

    assert forged_calls == []


def test_admission_ignores_rebound_event_sha_helper(monkeypatch) -> None:
    monkeypatch.setattr(
        controller_module,
        "_require_sha",
        lambda _value, *, field: HEAD_B,
    )

    class FakeApi:
        def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
            assert pr_number == 2008
            return HEAD_B, True

    assert controller_module.admit_current_head(
        api=FakeApi(),
        pr_number=2008,
        event_head_sha=HEAD_A,
    ) == CancellationResult(
        current_head=False,
        cancelled_run_ids=(),
    )


def test_admission_rejects_preentry_snapshot_helper_rebind(monkeypatch) -> None:
    forged_calls: list[int] = []

    def forged_snapshot(api, pr_number: int, **_kwargs) -> tuple[str, bool]:
        del api
        forged_calls.append(pr_number)
        return HEAD_A, True

    monkeypatch.setattr(
        controller_module,
        "_qualification_snapshot",
        forged_snapshot,
    )

    class FakeApi:
        def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
            raise AssertionError(f"forged pre-entry path must fail first: {pr_number}")

    with pytest.raises(
        CancellationError,
        match="pull request admission authority changed",
    ):
        controller_module.admit_current_head(
            api=FakeApi(),
            pr_number=2008,
            event_head_sha=HEAD_A,
        )

    assert forged_calls == []


def test_main_admission_does_not_trust_rebound_positive_int_helper(
    monkeypatch,
) -> None:
    requested: list[int] = []

    def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
        del self
        requested.append(pr_number)
        return HEAD_B, True

    monkeypatch.setattr(
        controller_module.GitHubApi,
        "live_pr_qualification",
        live_pr_qualification,
    )
    monkeypatch.setattr(
        controller_module,
        "_require_positive_int",
        lambda _value, *, field: 999,
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    assert controller_module.main(
        [
            "--pr-number",
            "2008",
            "--event-head-sha",
            HEAD_B,
            "--workflow-name",
            "CI",
            "--current-run-id",
            "123",
            "--admission-only",
        ]
    ) == 0

    assert requested == [2008]


def test_main_rejects_output_writer_rebind_from_live_qualification(
    monkeypatch,
) -> None:
    forged_calls: list[object] = []

    def forged_output_writer(result: object) -> None:
        forged_calls.append(result)

    def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
        del self
        assert pr_number == 2008
        monkeypatch.setattr(
            controller_module,
            "_write_github_output",
            forged_output_writer,
        )
        return HEAD_B, True

    monkeypatch.setattr(
        controller_module.GitHubApi,
        "live_pr_qualification",
        live_pr_qualification,
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    assert controller_module.main(
        [
            "--pr-number",
            "2008",
            "--event-head-sha",
            HEAD_B,
            "--workflow-name",
            "CI",
            "--current-run-id",
            "123",
            "--admission-only",
        ]
    ) == 2

    assert forged_calls == []


def test_positive_admission_is_revoked_by_head_move_during_confirmation() -> None:
    snapshots = iter(((HEAD_B, True), (HEAD_A, True)))

    class MovingApi:
        def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
            assert pr_number == 2008
            return next(snapshots)

    assert controller_module.admit_current_head(
        api=MovingApi(),
        pr_number=2008,
        event_head_sha=HEAD_B,
    ) == CancellationResult(
        current_head=False,
        cancelled_run_ids=(),
    )


def test_positive_admission_is_revoked_by_lifecycle_move_during_confirmation() -> None:
    snapshots = iter(((HEAD_B, True), (HEAD_B, False)))

    class MovingApi:
        def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
            assert pr_number == 2008
            return next(snapshots)

    assert controller_module.admit_current_head(
        api=MovingApi(),
        pr_number=2008,
        event_head_sha=HEAD_B,
    ) == CancellationResult(
        current_head=False,
        cancelled_run_ids=(),
    )

def test_admission_rejects_inflight_live_qualification_instance_shadow() -> None:
    forged_calls: list[int] = []

    class RebindingApi:
        def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
            assert pr_number == 2008

            def forged_resolver(next_pr_number: int) -> tuple[str, bool]:
                forged_calls.append(next_pr_number)
                return HEAD_B, True

            self.live_pr_qualification = forged_resolver
            return HEAD_B, True

    with pytest.raises(
        CancellationError,
        match="pull request qualification resolver authority changed",
    ):
        controller_module.admit_current_head(
            api=RebindingApi(),
            pr_number=2008,
            event_head_sha=HEAD_B,
        )

    assert forged_calls == []


def test_admission_rejects_inflight_live_qualification_class_rebind(
    monkeypatch,
) -> None:
    forged_calls: list[int] = []

    class RebindingApi:
        def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
            assert pr_number == 2008

            def forged_resolver(_self, next_pr_number: int) -> tuple[str, bool]:
                forged_calls.append(next_pr_number)
                return HEAD_B, True

            monkeypatch.setattr(
                RebindingApi,
                "live_pr_qualification",
                forged_resolver,
            )
            return HEAD_B, True

    with pytest.raises(
        CancellationError,
        match="pull request qualification resolver authority changed",
    ):
        controller_module.admit_current_head(
            api=RebindingApi(),
            pr_number=2008,
            event_head_sha=HEAD_B,
        )

    assert forged_calls == []


def test_admission_rejects_inflight_legacy_live_head_instance_shadow() -> None:
    forged_calls: list[int] = []

    class LegacyApi:
        def live_pr_head(self, pr_number: int) -> str:
            assert pr_number == 2008

            def forged_resolver(next_pr_number: int) -> str:
                forged_calls.append(next_pr_number)
                return HEAD_B

            self.live_pr_head = forged_resolver
            return HEAD_B

    with pytest.raises(
        CancellationError,
        match="pull request live-head resolver authority changed",
    ):
        controller_module.admit_current_head(
            api=LegacyApi(),
            pr_number=2008,
            event_head_sha=HEAD_B,
        )

    assert forged_calls == []

def _canonical_pr_payload(head_sha: str = HEAD_B) -> dict[str, object]:
    return {
        "state": "open",
        "draft": False,
        "head": {
            "sha": head_sha,
            "repo": {"full_name": "owner/repo"},
        },
        "base": {"repo": {"full_name": "owner/repo"}},
    }


def test_live_qualification_rejects_inflight_pull_request_reader_shadow() -> None:
    api = controller_module.GitHubApi(repository="owner/repo", token="token")
    forged_calls: list[int] = []

    def forged_pull_request(pr_number: int) -> dict[str, object]:
        forged_calls.append(pr_number)
        return _canonical_pr_payload(HEAD_B)

    def request(path: str, **_kwargs) -> object:
        assert path == "/pulls/2008"
        api._pull_request = forged_pull_request
        return _canonical_pr_payload(HEAD_B)

    api._request = request

    with pytest.raises(
        CancellationError,
        match="pull request reader authority changed",
    ):
        api.live_pr_qualification(2008)

    assert forged_calls == []


def test_pull_request_reader_rejects_inflight_request_transport_shadow() -> None:
    api = controller_module.GitHubApi(repository="owner/repo", token="token")
    forged_calls: list[str] = []

    def forged_request(path: str, **_kwargs) -> object:
        forged_calls.append(path)
        return _canonical_pr_payload(HEAD_B)

    def request(path: str, **_kwargs) -> object:
        assert path == "/pulls/2008"
        api._request = forged_request
        return _canonical_pr_payload(HEAD_B)

    api._request = request

    with pytest.raises(
        CancellationError,
        match="pull request transport authority changed",
    ):
        api.live_pr_qualification(2008)

    assert forged_calls == []


def test_pull_request_reader_rejects_inflight_request_default_rebase(
    monkeypatch,
) -> None:
    api = controller_module.GitHubApi(repository="owner/repo", token="token")
    request_impl = controller_module.GitHubApi._request
    defaults = request_impl.__kwdefaults__
    assert defaults is not None

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return (
                b'{"state":"open","draft":false,'
                b'"head":{"sha":"' + HEAD_B.encode("ascii")
                + b'","repo":{"full_name":"owner/repo"}},'
                b'"base":{"repo":{"full_name":"owner/repo"}}}'
            )

    def mutating_urlopen(request, *, timeout: int):
        assert timeout == 20
        assert request.full_url.endswith("/pulls/2008")
        monkeypatch.setitem(defaults, "_json_parse_int", str)
        return FakeResponse()

    monkeypatch.setitem(
        request_impl.__globals__,
        "urlopen",
        mutating_urlopen,
    )

    with pytest.raises(
        CancellationError,
        match="pull request transport authority changed",
    ):
        api.live_pr_qualification(2008)


def test_legacy_live_head_rejects_inflight_pull_request_reader_shadow() -> None:
    api = controller_module.GitHubApi(repository="owner/repo", token="token")
    forged_calls: list[int] = []

    def forged_pull_request(pr_number: int) -> dict[str, object]:
        forged_calls.append(pr_number)
        return _canonical_pr_payload(HEAD_A)

    def request(path: str, **_kwargs) -> object:
        assert path == "/pulls/2008"
        api._pull_request = forged_pull_request
        return _canonical_pr_payload(HEAD_B)

    api._request = request

    with pytest.raises(
        CancellationError,
        match="pull request reader authority changed",
    ):
        api.live_pr_head(2008)

    assert forged_calls == []

def _main_admission_args() -> list[str]:
    return [
        "--pr-number",
        "2008",
        "--event-head-sha",
        HEAD_B,
        "--workflow-name",
        "CI",
        "--current-run-id",
        "123",
        "--admission-only",
    ]


def _main_cancel_args() -> list[str]:
    return [
        "--pr-number",
        "2008",
        "--event-head-sha",
        HEAD_B,
        "--workflow-name",
        "CI",
        "--current-run-id",
        "123",
    ]


def test_main_rejects_preentry_admission_dispatch_rebind(monkeypatch, tmp_path) -> None:
    forged_calls: list[str] = []

    def forged_admit(**_kwargs) -> CancellationResult:
        forged_calls.append("admit")
        return CancellationResult(current_head=True, cancelled_run_ids=())

    output = tmp_path / "github-output.txt"
    monkeypatch.setattr(controller_module, "admit_current_head", forged_admit)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert controller_module.main(_main_admission_args()) == 2
    assert forged_calls == []
    assert not output.exists()


def test_main_rejects_preentry_cancel_dispatch_rebind(monkeypatch, tmp_path) -> None:
    forged_calls: list[str] = []

    def forged_cancel(**_kwargs) -> CancellationResult:
        forged_calls.append("cancel")
        return CancellationResult(current_head=True, cancelled_run_ids=(999,))

    output = tmp_path / "github-output.txt"
    monkeypatch.setattr(controller_module, "cancel_superseded", forged_cancel)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert controller_module.main(_main_cancel_args()) == 2
    assert forged_calls == []
    assert not output.exists()


def test_main_rejects_preentry_output_writer_rebind(monkeypatch, tmp_path) -> None:
    forged_calls: list[object] = []

    def forged_output(result: object) -> None:
        forged_calls.append(result)

    output = tmp_path / "github-output.txt"
    monkeypatch.setattr(controller_module, "_write_github_output", forged_output)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert controller_module.main(_main_admission_args()) == 2
    assert forged_calls == []
    assert not output.exists()


def test_main_rejects_preentry_api_type_rebind(monkeypatch) -> None:
    forged_calls: list[str] = []

    class ForgedApi:
        def __init__(self, **_kwargs) -> None:
            forged_calls.append("init")

    monkeypatch.setattr(controller_module, "GitHubApi", ForgedApi)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    assert controller_module.main(_main_admission_args()) == 2
    assert forged_calls == []


def test_main_rejects_preentry_live_qualification_rebind(
    monkeypatch,
    tmp_path,
) -> None:
    forged_calls: list[int] = []

    def forged_live_qualification(_self, pr_number: int) -> tuple[str, bool]:
        forged_calls.append(pr_number)
        return HEAD_B, True

    output = tmp_path / "github-output.txt"
    monkeypatch.setattr(
        controller_module.GitHubApi,
        "live_pr_qualification",
        forged_live_qualification,
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert controller_module.main(_main_admission_args()) == 2
    assert forged_calls == []
    assert not output.exists()


def test_main_rejects_preentry_request_method_rebind(
    monkeypatch,
    tmp_path,
) -> None:
    forged_calls: list[str] = []

    def forged_request(_self, path: str, **_kwargs) -> object:
        forged_calls.append(path)
        return _canonical_pr_payload(HEAD_B)

    output = tmp_path / "github-output.txt"
    monkeypatch.setattr(
        controller_module.GitHubApi,
        "_request",
        forged_request,
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert controller_module.main(_main_admission_args()) == 2
    assert forged_calls == []
    assert not output.exists()


def test_main_rejects_preentry_urlopen_rebind(monkeypatch, tmp_path) -> None:
    forged_calls: list[object] = []

    def forged_urlopen(request: object, *, timeout: int) -> object:
        forged_calls.extend((request, timeout))
        raise AssertionError("forged transport must not execute")

    output = tmp_path / "github-output.txt"
    monkeypatch.setattr(controller_module, "urlopen", forged_urlopen)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert controller_module.main(_main_admission_args()) == 2
    assert forged_calls == []
    assert not output.exists()


def test_main_rejects_preentry_argument_parser_rebind(
    monkeypatch,
    tmp_path,
) -> None:
    forged_calls: list[str] = []

    class ForgedParser:
        def __init__(self, *_args, **_kwargs) -> None:
            forged_calls.append("parser")

    output = tmp_path / "github-output.txt"
    monkeypatch.setattr(
        controller_module.argparse,
        "ArgumentParser",
        ForgedParser,
    )
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert controller_module.main(_main_admission_args()) == 2
    assert forged_calls == []
    assert not output.exists()


def test_main_rejects_preentry_os_module_rebind(monkeypatch, tmp_path) -> None:
    forged_calls: list[str] = []

    class ForgedOs:
        @property
        def environ(self):
            forged_calls.append("environ")
            return {}

    output = tmp_path / "github-output.txt"
    monkeypatch.setattr(controller_module, "os", ForgedOs())
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert controller_module.main(_main_admission_args()) == 2
    assert forged_calls == []
    assert not output.exists()


def test_main_rejects_preentry_admission_code_mutation(monkeypatch) -> None:
    target = controller_module.admit_current_head

    def forged_admit(**_kwargs) -> CancellationResult:
        return CancellationResult(current_head=True, cancelled_run_ids=())

    monkeypatch.setattr(target, "__code__", forged_admit.__code__)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    assert controller_module.main(_main_admission_args()) == 2


def test_main_orchestration_roots_are_not_caller_injectable() -> None:
    forged_calls: list[str] = []

    def forged_admit(**_kwargs) -> CancellationResult:
        forged_calls.append("admit")
        return CancellationResult(current_head=True, cancelled_run_ids=())

    def forged_cancel(**_kwargs) -> CancellationResult:
        forged_calls.append("cancel")
        return CancellationResult(current_head=True, cancelled_run_ids=(999,))

    def forged_output(_result: object) -> None:
        forged_calls.append("output")

    class ForgedApi:
        def __init__(self, **_kwargs) -> None:
            forged_calls.append("api")

    forged_globals = {
        "admit_current_head": forged_admit,
        "cancel_superseded": forged_cancel,
        "_write_github_output": forged_output,
        "GitHubApi": ForgedApi,
    }

    assert controller_module.main.__kwdefaults__ is None
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        controller_module.main(
            _main_admission_args(),
            _module_globals=forged_globals,
            _admit_impl=forged_admit,
            _admit_code=forged_admit.__code__,
            _cancel_impl=forged_cancel,
            _cancel_code=forged_cancel.__code__,
            _output_writer=forged_output,
            _output_writer_code=forged_output.__code__,
            _api_type=ForgedApi,
            _api_init=ForgedApi.__init__,
            _api_init_code=ForgedApi.__init__.__code__,
        )
    assert forged_calls == []


def test_main_kwdefaults_metadata_cannot_rebase_orchestration(
    monkeypatch,
) -> None:
    forged_calls: list[str] = []

    def forged_admit(**_kwargs) -> CancellationResult:
        forged_calls.append("admit")
        return CancellationResult(current_head=True, cancelled_run_ids=())

    monkeypatch.setattr(
        controller_module.main,
        "__kwdefaults__",
        {"_admit_impl": forged_admit, "_admit_code": forged_admit.__code__},
    )
    monkeypatch.setattr(controller_module, "admit_current_head", forged_admit)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    assert controller_module.main(_main_admission_args()) == 2
    assert forged_calls == []



def test_main_has_no_implicit_argv_default_and_entrypoint_passes_sys_argv() -> None:
    assert controller_module.main.__defaults__ is None

    source = Path("scripts/cancel_superseded_pr_workflow_runs.py").read_text(
        encoding="utf-8"
    )
    assert "raise SystemExit(main(sys.argv[1:]))" in source
    assert "raise SystemExit(main())" not in source



def test_main_rejects_inplace_cancel_kwdefault_rebase(monkeypatch) -> None:
    defaults = controller_module.cancel_superseded.__kwdefaults__
    assert defaults is not None
    assert defaults["cancel_same_head"] is None

    monkeypatch.setitem(defaults, "cancel_same_head", True)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    assert controller_module.main(_main_cancel_args()) == 2


def test_main_rejects_replaced_admission_kwdefault_mapping(monkeypatch) -> None:
    defaults = controller_module.admit_current_head.__kwdefaults__
    assert defaults is not None

    replacement = dict(defaults)
    monkeypatch.setattr(
        controller_module.admit_current_head,
        "__kwdefaults__",
        replacement,
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    assert controller_module.main(_main_admission_args()) == 2

def test_admission_rejects_nested_snapshot_kwdefault_rebase(monkeypatch) -> None:
    defaults = controller_module._qualification_snapshot.__kwdefaults__
    assert defaults is not None
    forged_calls: list[object] = []

    def forged_reader(qualification: object) -> tuple[str, bool]:
        forged_calls.append(qualification)
        return HEAD_A, True

    monkeypatch.setitem(
        defaults,
        "_qualification_state_reader",
        forged_reader,
    )

    class FakeApi:
        def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
            raise AssertionError(f"nested authority drift must fail first: {pr_number}")

    with pytest.raises(
        CancellationError,
        match="pull request admission authority changed",
    ):
        controller_module.admit_current_head(
            api=FakeApi(),
            pr_number=2008,
            event_head_sha=HEAD_A,
        )

    assert forged_calls == []

