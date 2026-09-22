from autosport.historical_governance import bundle_corpus_main, corpus_main


_UKRAINIAN_FAILURE = (
    "Не вдалося завершити операцію з історичним корпусом. "
    "Операцію безпечно зупинено."
)


def test_historical_corpus_fail_closed_is_ukrainian_first_and_keeps_machine_diagnostic(capsys) -> None:
    assert corpus_main([]) == 3

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == _UKRAINIAN_FAILURE
    assert lines[1] == (
        "historical_corpus=FAIL_CLOSED "
        "error=--governance-proof is required before rights provenance can be verified"
    )


def test_bundle_historical_corpus_fail_closed_is_ukrainian_first_and_keeps_machine_diagnostic(capsys) -> None:
    assert bundle_corpus_main([]) == 3

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == _UKRAINIAN_FAILURE
    assert lines[1] == (
        "historical_bundle_corpus=FAIL_CLOSED "
        "error=--governance-proof is required before rights provenance can be verified"
    )
