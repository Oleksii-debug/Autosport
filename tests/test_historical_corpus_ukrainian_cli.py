from pathlib import Path

from autosport.historical_bundle_corpus import build_parser as build_bundle_parser
from autosport.historical_corpus import build_parser as build_corpus_parser


def test_historical_corpus_help_is_ukrainian_first_and_options_remain_stable() -> None:
    parser = build_corpus_parser()
    help_text = parser.format_help()

    assert "Збирає вибрані автентифіковані історичні знімки" in help_text
    assert "перевіркою прав і строків зберігання" in help_text
    assert "каузальний момент розкриття результату" in help_text
    assert "--snapshot" in help_text
    assert "--governance-proof" in help_text
    assert "--outcome-reveal-after" in help_text

    args = parser.parse_args(
        [
            "--snapshot",
            "market.jsonl",
            "evidence.json",
            "--results",
            "results.json",
            "--governance-proof",
            "governance.json",
            "--output",
            "corpus",
            "--name",
            "test corpus",
            "--outcome-reveal-after",
            "2026-09-01T00:00:00Z",
            "--imported-at",
            "2026-09-02T00:00:00Z",
        ]
    )
    assert args.snapshot == [["market.jsonl", "evidence.json"]]
    assert args.results == Path("results.json")
    assert args.governance_proof == Path("governance.json")
    assert args.output == Path("corpus")
    assert args.name == "test corpus"


def test_bundle_corpus_help_is_ukrainian_first_and_options_remain_stable() -> None:
    parser = build_bundle_parser()
    help_text = parser.format_help()

    assert "Перевіряє один незмінний bundle історичного придбання" in help_text
    assert "очікуваний SHA-256 усього bundle" in help_text
    assert "новий каталог вихідного керованого корпусу" in help_text
    assert "--bundle-dir" in help_text
    assert "--expected-bundle-sha256" in help_text
    assert "--output-dir" in help_text

    args = parser.parse_args(
        [
            "--bundle-dir",
            "bundle",
            "--expected-bundle-sha256",
            "a" * 64,
            "--results",
            "results.json",
            "--governance-proof",
            "governance.json",
            "--output-dir",
            "corpus",
            "--name",
            "test corpus",
            "--outcome-reveal-after",
            "2026-09-01T00:00:00Z",
            "--imported-at",
            "2026-09-02T00:00:00Z",
        ]
    )
    assert args.bundle_dir == Path("bundle")
    assert args.expected_bundle_sha256 == "a" * 64
    assert args.results == Path("results.json")
    assert args.governance_proof == Path("governance.json")
    assert args.output_dir == Path("corpus")
