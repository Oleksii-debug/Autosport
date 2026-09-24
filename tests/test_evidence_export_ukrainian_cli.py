from pathlib import Path

from autosport.evidence_export import build_parser, build_verify_parser


def test_export_evidence_help_is_ukrainian_first_without_renaming_machine_options() -> None:
    parser = build_parser()
    help_text = parser.format_help()

    assert "Експортує детермінований маніфест доказів" in help_text
    assert "наявний робочий простір Autosport" in help_text
    assert "файл призначення для JSON-маніфесту доказів" in help_text
    assert "РОБОЧИЙ_ПРОСТІР" in help_text
    assert "JSON_ФАЙЛ" in help_text
    assert "--output" in help_text

    args = parser.parse_args(["workspace", "--output", "evidence.json"])
    assert args.workspace == Path("workspace")
    assert args.output == Path("evidence.json")


def test_verify_evidence_help_is_ukrainian_first_without_renaming_machine_options() -> None:
    parser = build_verify_parser()
    help_text = parser.format_help()

    assert "Перевіряє маніфест доказів Autosport" in help_text
    assert "JSON-маніфест доказів для перевірки" in help_text
    assert "наявний робочий простір Autosport" in help_text
    assert "МАНІФЕСТ" in help_text
    assert "РОБОЧИЙ_ПРОСТІР" in help_text
    assert "--workspace" in help_text

    args = parser.parse_args(["evidence.json", "--workspace", "workspace"])
    assert args.manifest == Path("evidence.json")
    assert args.workspace == Path("workspace")
