from __future__ import annotations

import re
from decimal import Decimal
from types import SimpleNamespace

import pytest

from autosport.domain import TicketLeg
from autosport.localization import (
    CATALOG_VERSION,
    DEFAULT_LOCALE,
    catalog,
    require_keys,
    text,
)
from autosport.paper import PaperBook
from autosport.ui_model import (
    observation_quote_lines,
    observation_summary,
    result_summary,
    ticket_lines,
)


_CRITICAL_UI_KEYS = {
    "ui.app.title",
    "ui.dialog.title",
    "ui.label.strategy",
    "ui.label.speed",
    "ui.label.live_mode",
    "ui.label.live_quotes",
    "ui.label.tickets",
    "ui.label.evaluation",
    "ui.label.log",
    "ui.button.research_plan",
    "ui.button.choose_dataset",
    "ui.button.run_replay",
    "ui.button.repair_workspace",
    "ui.button.live_refresh",
    "ui.speed.event_driven",
    "ui.speed.realtime",
    "ui.speed.10x",
    "ui.speed.100x",
    "ui.speed.1000x",
    "ui.live_mode.public_preview",
    "ui.live_mode.api_key",
    "ui.strategy.display",
    "ui.status.startup.ready",
    "ui.status.startup.recovery_required",
    "ui.status.dataset.none",
    "ui.status.research_plan.baseline",
    "ui.status.live.never",
    "ui.status.live_quotes.empty",
    "ui.status.evaluation.empty",
    "ui.accessibility.strategy.name",
    "ui.accessibility.strategy.description",
    "ui.accessibility.research_plan.name",
    "ui.accessibility.research_plan.description",
    "ui.accessibility.choose_dataset.name",
    "ui.accessibility.choose_dataset.description",
    "ui.accessibility.run_replay.name",
    "ui.accessibility.run_replay.description",
    "ui.accessibility.repair_workspace.name",
    "ui.accessibility.repair_workspace.description",
    "ui.accessibility.replay_speed.name",
    "ui.accessibility.replay_speed.description",
    "ui.accessibility.live_mode.name",
    "ui.accessibility.live_mode.description",
    "ui.accessibility.live_refresh.name",
    "ui.accessibility.live_refresh.description",
    "ui.accessibility.live_quotes.name",
    "ui.accessibility.live_quotes.description",
    "ui.accessibility.tickets.name",
    "ui.accessibility.tickets.description",
    "ui.accessibility.evaluation.name",
    "ui.accessibility.evaluation.description",
    "ui.accessibility.log.name",
    "ui.accessibility.log.description",
    "ui.accessibility.bankroll.name",
    "ui.accessibility.bankroll.description",
}

_RUNTIME_RECOVERY_KEYS = {
    "ui.status.bank.pending",
    "ui.status.bank.current",
    "ui.status.windows.economic_unavailable",
    "ui.status.windows.strategy_recovery_busy",
    "ui.status.windows.research_plan_recovery_busy",
    "ui.status.windows.dataset_recovery_busy",
    "ui.status.windows.live_recovery_busy",
    "ui.status.windows.live_recovery_blocked",
    "ui.status.recovery.dataset_busy",
    "ui.status.recovery.replay_busy",
    "ui.status.recovery.live_busy",
    "ui.status.recovery.already_busy",
    "ui.error.recovery.configuration",
    "ui.status.recovery.configuration_rejected",
    "ui.error.recovery.teardown",
    "ui.status.recovery.teardown_blocked",
    "ui.status.recovery.start_failed",
    "ui.status.recovery.running",
    "ui.log.recovery.started",
    "ui.error.recovery.worker",
    "ui.status.recovery.blocked",
    "ui.status.recovery.no_result",
    "ui.error.recovery.identity_mismatch",
    "ui.status.recovery.identity_mismatch",
    "ui.recovery.summary",
    "ui.status.recovery.unresolved_suffix",
    "ui.warning.recovery.unresolved",
    "ui.status.recovery.ready_suffix",
    "ui.info.recovery.complete",
    "ui.status.research_plan.identity_suffix",
    "ui.status.replay.recovery_busy",
    "ui.status.replay.recovery_required",
    "ui.evaluation.replay_failed",
    "ui.status.replay.failed_recovery",
    "ui.evaluation.no_terminal_result",
    "ui.status.replay.no_terminal_result",
    "ui.evaluation.reopen_failed",
    "ui.error.replay.reopen",
    "ui.status.replay.reopen_blocked",
    "ui.status.close.recovery_busy",
}


_LANGUAGE_NEUTRAL_CRITICAL_VALUES = {
    "ui.speed.10x": "10×",
    "ui.speed.100x": "100×",
    "ui.speed.1000x": "1000×",
}
_FORMAT_FIELD = re.compile(r"\{[^{}]*\}")
_CYRILLIC_LETTER = re.compile(r"[\u0400-\u04FF]")
_ENGLISH_UI_DIRECTIVE = re.compile(
    r"\b(?:press|click|retry|reopen|settings|please|confirm|submit|place|cancel|try\s+again|failed\s+to|unable\s+to)\b",
    re.IGNORECASE,
)


_UPPERCASE_PROSE_SPAN = re.compile(r"\b[A-Z]{2,}(?:\s+[A-Z]{2,})+\b")
_ALLOWED_UPPERCASE_TECHNICAL_WORDS = frozenset(
    {"API", "UIA", "NVDA", "UTC", "SHA", "ID", "STOP", "PAPER", "SHADOW", "LIVE"}
)


def _has_untranslated_uppercase_prose(value: str) -> bool:
    # Technical abbreviations and the native STOP command are not prose.
    # Two or more other adjacent uppercase words are a product-copy escape,
    # even when one Cyrillic character was appended to launder the string.
    for match in _UPPERCASE_PROSE_SPAN.finditer(value):
        words = match.group(0).split()
        untranslated = [
            word for word in words if word not in _ALLOWED_UPPERCASE_TECHNICAL_WORDS
        ]
        if len(untranslated) >= 2:
            return True
    return False


def _assert_critical_ukrainian_template(key: str, value: str) -> None:
    stripped = value.strip()
    assert stripped, f"{key} resolved to an empty/whitespace critical value"
    assert stripped != key, f"{key} resolved by echoing its localization key"

    neutral_value = _LANGUAGE_NEUTRAL_CRITICAL_VALUES.get(key)
    if neutral_value is not None:
        assert stripped == neutral_value, (
            f"{key} changed from its exact language-neutral technical value: {value!r}"
        )
        return

    language_text = _FORMAT_FIELD.sub("", stripped)
    assert _CYRILLIC_LETTER.search(language_text), (
        f"{key} has no Ukrainian/Cyrillic presentation signal: {value!r}"
    )
    assert not _ENGLISH_UI_DIRECTIVE.search(language_text), (
        f"{key} contains an English-only critical UI directive: {value!r}"
    )
    assert not _has_untranslated_uppercase_prose(language_text), (
        f"{key} contains untranslated uppercase operator prose: {value!r}"
    )


def test_critical_catalog_is_fail_closed_against_blank_key_echo_and_english_fallback() -> None:
    messages = catalog()
    guarded_keys = _CRITICAL_UI_KEYS | _RUNTIME_RECOVERY_KEYS
    require_keys(guarded_keys)

    # The bounded manifests deliberately include visible controls, UIA/NVDA
    # presentation resources, and startup/recovery status/error text, so all of
    # those critical paths share the same fail-closed language guard.
    assert any(key.startswith("ui.button.") for key in guarded_keys)
    assert any(key.startswith("ui.accessibility.") for key in guarded_keys)
    assert any(key.startswith("ui.error.recovery.") for key in guarded_keys)
    assert any(key.startswith("ui.status.recovery.") for key in guarded_keys)

    for key in sorted(guarded_keys):
        _assert_critical_ukrainian_template(key, messages[key])


def test_critical_ukrainian_guard_allows_technical_tokens_only_with_ukrainian_context() -> None:
    _assert_critical_ukrainian_template(
        "ui.example.provider_status",
        "Статус Betfair API: доступний; час UTC.",
    )
    _assert_critical_ukrainian_template(
        "ui.example.technical_context",
        "Betfair API UIA NVDA UTC SHA-256: стан доступний.",
    )

    with pytest.raises(AssertionError, match="no Ukrainian/Cyrillic presentation signal"):
        _assert_critical_ukrainian_template(
            "ui.example.provider_status",
            "Betfair API status: ready; time UTC.",
        )
    # A stray Cyrillic character must not launder an English operator command.
    for english_with_cyrillic in (
        "Press Retry і",
        "Error: press Retry and reopen Settings — і",
        "Please click тут",
        "CONFIRM REAL BET і",
        "SUBMIT REAL BET і",
        "PLACE REAL BET і",
        "CANCEL REAL BET і",
    ):
        with pytest.raises(AssertionError, match="English-only critical UI directive"):
            _assert_critical_ukrainian_template(
                "ui.example.unsafe_english_directive", english_with_cyrillic
            )
    for untranslated_uppercase in (
        "START REAL BET і",
        "DELETE ALL DATA і",
        "EXPORT PRIVATE DATA і",
    ):
        with pytest.raises(
            AssertionError, match="untranslated uppercase operator prose"
        ):
            _assert_critical_ukrainian_template(
                "ui.example.unsafe_uppercase_prose", untranslated_uppercase
            )
    with pytest.raises(AssertionError, match="empty/whitespace"):
        _assert_critical_ukrainian_template("ui.example.blank", "   ")
    with pytest.raises(AssertionError, match="echoing its localization key"):
        _assert_critical_ukrainian_template("ui.example.echo", "ui.example.echo")
    with pytest.raises(AssertionError, match="exact language-neutral technical value"):
        _assert_critical_ukrainian_template("ui.speed.10x", "Fast")


def test_catalog_is_versioned_ukrainian_default_and_fails_closed() -> None:
    assert DEFAULT_LOCALE == "uk-UA"
    assert CATALOG_VERSION == 8
    assert text("ui.ticket.empty") == "Паперові квитки ще відсутні."
    assert text("ui.boolean.true") == "так"
    assert text("ui.boolean.false") == "ні"

    require_keys(
        _CRITICAL_UI_KEYS
        | _RUNTIME_RECOVERY_KEYS
        | {
            "ui.result.summary",
            "ui.evaluation.bankroll",
            "ui.evaluation.metrics",
            "ui.evaluation.portfolio",
            "ui.evaluation.truth",
            "ui.ticket.row",
            "ui.observation.summary",
            "ui.observation.quote",
        }
    )

    with pytest.raises(ValueError, match="unsupported locale"):
        catalog("en-US")
    with pytest.raises(KeyError, match="missing localization key"):
        text("ui.missing")
    with pytest.raises(KeyError, match="missing localization value"):
        text("ui.result.summary", run_id="r")
    with pytest.raises(KeyError, match="missing localization keys"):
        require_keys({"ui.missing"})


def test_critical_catalog_strings_are_exact_ukrainian_presentation() -> None:
    assert text("ui.app.title") == "Автоспорт — аналітична програма для Windows"
    assert text("ui.dialog.title") == "Автоспорт"
    assert text("ui.button.run_replay") == "Запустити паперовий повтор"
    assert text("ui.speed.event_driven") == "Подієвий — максимально швидко"
    assert text("ui.live_mode.public_preview") == "Публічний перегляд — без ключа"
    assert text("ui.accessibility.strategy.name") == "Стратегія повтору"
    assert text("ui.accessibility.live_quotes.name") == "Поточні котирування"
    assert text("ui.accessibility.bankroll.name") == "Віртуальний банк"


def test_whole_product_chrome_has_no_version_finish_line_token() -> None:
    assert "V1" not in text("ui.app.title")
    assert "V1" not in text("ui.dialog.title")


def test_remaining_runtime_presentation_residuals_are_localized_without_mutating_sha() -> None:
    sha_prefix = "abcdef123456"
    legacy_plan_identity = f"; plan={sha_prefix}…"

    replay = text(
        "ui.status.replay.running",
        strategy_id="research-replay-v1",
        plan_identity=legacy_plan_identity,
    )
    recovery = text(
        "ui.status.recovery.running",
        strategy_id="research-replay-v1",
        plan_identity=legacy_plan_identity,
    )

    assert f"; план={sha_prefix}…" in replay
    assert f"; план={sha_prefix}…" in recovery
    assert "; plan=" not in replay
    assert "; plan=" not in recovery
    assert sha_prefix in replay
    assert sha_prefix in recovery

    live = text("ui.status.live.read_only_running")
    assert "PaperBook" not in live
    assert "Паперовий облік" in live

    close = text("ui.status.close.replay_busy")
    assert "commit" not in close
    assert "фіксацію транзакції" in close


def test_runtime_recovery_catalog_preserves_raw_identity_and_economic_values() -> None:
    bankroll = text(
        "ui.status.bank.current",
        balance=Decimal("10000.25"),
        committed_stake=Decimal("17.50"),
        strategy_id="research-replay-v1/raw",
        workspace=r"C:\raw\workspace-17",
    )
    assert "10000.25" in bankroll
    assert "17.50" in bankroll
    assert "research-replay-v1/raw" in bankroll
    assert r"C:\raw\workspace-17" in bankroll

    mismatch = text(
        "ui.error.recovery.identity_mismatch",
        expected_workspace=r"C:\expected",
        expected_strategy_id="baseline-v1",
        received_workspace="PosixPath('/raw/provider/path')",
        received_strategy_id="'provider-strategy:raw'",
    )
    assert r"C:\expected" in mismatch
    assert "baseline-v1" in mismatch
    assert "PosixPath('/raw/provider/path')" in mismatch
    assert "'provider-strategy:raw'" in mismatch

    recovery_error = text(
        "ui.error.recovery.worker",
        detail="RuntimeError: provider_raw_detail=ABC-123",
    )
    assert "RuntimeError: provider_raw_detail=ABC-123" in recovery_error


def test_result_summary_localizes_labels_but_preserves_raw_economic_values() -> None:
    result = SimpleNamespace(
        replay=SimpleNamespace(run_id="abcdef123456", event_count=7),
        balance=Decimal("10000.25"),
        evaluation=SimpleNamespace(net_profit=Decimal("-12.50")),
        settled_ticket_ids=("ticket-1", "ticket-2"),
        portfolio=SimpleNamespace(
            mode="exact",
            worst_case=Decimal("-50.00"),
            best_case=Decimal("75.00"),
        ),
    )

    rendered = result_summary(result)

    assert rendered.startswith("Повтор abcdef12:")
    assert "подій=7" in rendered
    assert "баланс=10000.25" in rendered
    assert "чистий_результат=-12.50" in rendered
    assert "портфель=exact" in rendered
    assert "найгірше=-50.00" in rendered
    assert "найкраще=75.00" in rendered


def test_ticket_lines_preserve_canonical_leg_identity_and_decimal_values() -> None:
    book = PaperBook("100")
    leg = TicketLeg(
        event_id="event:raw-1",
        market_id="market:raw-2",
        selection_id="selection:raw-3",
        locked_odds=Decimal("2.10"),
    )
    ticket = book.open_ticket(
        (leg,),
        Decimal("25.50"),
        placed_at="2026-09-16T10:00:00+00:00",
    )
    book.settle(
        ticket.ticket_id,
        {leg.quote_key},
        settled_at="2026-09-16T10:01:00+00:00",
    )
    session = SimpleNamespace(book=book)

    rendered = ticket_lines(session)

    assert len(rendered) == 1
    assert rendered[0].startswith("WON | ставка 25.50 | коефіцієнт 2.10 | виплата 53.5500 | ")
    assert "event:raw-1/market:raw-2/selection:raw-3@2.10" in rendered[0]

    empty_session = SimpleNamespace(book=PaperBook("100"))
    assert ticket_lines(empty_session) == ["Паперові квитки ще відсутні."]


def test_observation_presentation_is_ukrainian_without_mutating_provider_identity() -> None:
    event = SimpleNamespace(
        event_id="evt-provider-1",
        market_type=SimpleNamespace(value="MATCH_WINNER"),
        market_id="market-provider-2",
        selection_id="selection-provider-3",
        decimal_odds=Decimal("1.91"),
        source_ts="2026-09-16T10:00:00Z",
    )
    result = SimpleNamespace(
        stats=SimpleNamespace(
            source_id="provider-raw-id",
            received=3,
            accepted=2,
            rejected=1,
            quality_flags=(),
        ),
        health=SimpleNamespace(status="HEALTHY"),
        current_quotes=(event,),
    )

    summary = observation_summary(result)
    quote = observation_quote_lines(result)[0]

    assert summary.startswith("Поточний знімок: джерело=provider-raw-id;")
    assert "отримано=3" in summary
    assert "прийнято=2" in summary
    assert "прапорці якості=немає" in summary
    assert "evt-provider-1 | MATCH_WINNER | market-provider-2 | selection-provider-3" in quote
    assert "коефіцієнт 1.91" in quote
    assert "час джерела 2026-09-16T10:00:00Z" in quote

    empty_result = SimpleNamespace(current_quotes=())
    assert observation_quote_lines(empty_result) == ["Поточні котирування ще відсутні."]


# Test-owned language conformance boundary for issue #1240. This deliberately
# proves presence of Ukrainian presentation rather than banning Latin/ASCII:
# canonical provider/protocol tokens remain valid inside Ukrainian context.
_UKRAINIAN_PRESENTATION_LETTERS = frozenset(
    "АБВГҐДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯ"
    "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
)

# These controls are intentionally language-neutral numeric mode tokens.
_CRITICAL_TECHNICAL_ONLY_KEYS = {
    "ui.speed.realtime",
    "ui.speed.10x",
    "ui.speed.100x",
    "ui.speed.1000x",
}

# Latin-script tokens are allowed only when their shape or exact identity is
# technical. Product prose around those tokens must remain Ukrainian-first.
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:/\\+\-]*")
_ALLOWED_TECHNICAL_LATIN_TOKENS = frozenset(
    {
        "API",
        "Autosport",
        "Betfair",
        "Control",
        "ID",
        "NVDA",
        "PaperBook",
        "Provider",
        "SHA",
        "Tk",
        "UIA",
        "UTC",
        "Windows",
    }
)


def _untranslated_latin_product_words(value: str) -> tuple[str, ...]:
    words: list[str] = []
    for token in _LATIN_TOKEN_RE.findall(value):
        if token in _ALLOWED_TECHNICAL_LATIN_TOKENS:
            continue
        if any(character.isdigit() for character in token):
            continue
        if any(character in "_.:/\\+-" for character in token):
            continue
        if token.isupper():
            continue
        if any(character.isupper() for character in token[1:]):
            continue
        words.append(token)
    return tuple(words)


def _assert_ukrainian_critical_presentation(key: str, value: str) -> None:
    assert value.strip(), f"{key} resolved to empty/whitespace presentation"
    assert value != key, f"{key} echoed the localization key instead of presentation"
    assert any(
        character in _UKRAINIAN_PRESENTATION_LETTERS for character in value
    ), f"{key} has no Ukrainian presentation text: {value!r}"
    assert not _ENGLISH_UI_DIRECTIVE.search(value), (
        f"{key} contains an English-only critical UI directive: {value!r}"
    )
    assert not _has_untranslated_uppercase_prose(value), (
        f"{key} contains untranslated uppercase operator prose: {value!r}"
    )
    untranslated = _untranslated_latin_product_words(value)
    assert not untranslated, (
        f"{key} contains untranslated Latin product words: {untranslated!r}; "
        f"value={value!r}"
    )


def test_critical_visible_and_uia_surfaces_require_ukrainian_presentation() -> None:
    messages = catalog()

    require_keys(_CRITICAL_UI_KEYS)
    for key in sorted(_CRITICAL_UI_KEYS - _CRITICAL_TECHNICAL_ONLY_KEYS):
        _assert_ukrainian_critical_presentation(key, messages[key])

    for key in sorted(_CRITICAL_TECHNICAL_ONLY_KEYS):
        value = messages[key]
        assert value.strip(), f"{key} resolved to empty/whitespace technical token"
        assert value != key, f"{key} echoed the localization key"


def test_critical_language_guard_allows_canonical_technical_tokens_in_ukrainian_context() -> None:
    allowed_samples = (
        "Betfair API: помилка автентифікації; перевірте ключ.",
        "Час UTC; SHA-256=abcdef123456.",
        "Provider-ID=raw-17; стан перевірено українською мовою.",
        "NVDA UIA та STOP: українська семантика доступна.",
    )
    for index, sample in enumerate(allowed_samples):
        _assert_ukrainian_critical_presentation(f"sample.{index}", sample)

    with pytest.raises(AssertionError, match="no Ukrainian presentation text"):
        _assert_ukrainian_critical_presentation(
            "sample.english_only",
            "Retry Betfair API request after timeout.",
        )

    # All-uppercase English commands must not bypass the critical UI gate
    # merely because the display string contains one Ukrainian letter.
    for english_uppercase_command in (
        "CONFIRM REAL BET і",
        "SUBMIT REAL BET і",
        "PLACE REAL BET і",
        "CANCEL REAL BET і",
    ):
        with pytest.raises(AssertionError, match="English-only critical UI directive"):
            _assert_ukrainian_critical_presentation(
                "sample.unsafe_uppercase_command", english_uppercase_command
            )

    for untranslated_uppercase in (
        "START REAL BET і",
        "DELETE ALL DATA і",
        "EXPORT PRIVATE DATA і",
    ):
        with pytest.raises(
            AssertionError, match="untranslated uppercase operator prose"
        ):
            _assert_ukrainian_critical_presentation(
                "sample.unsafe_uppercase_prose", untranslated_uppercase
            )

    with pytest.raises(AssertionError, match="English-only critical UI directive"):
        _assert_ukrainian_critical_presentation(
            "sample.mixed_fallback",
            "Betfair API: Retry request після помилки.",
        )
