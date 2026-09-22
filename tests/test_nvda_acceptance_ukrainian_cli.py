from pathlib import Path

from autosport.nvda_acceptance import _template_parser, _verify_parser


def test_nvda_template_help_is_ukrainian_first_without_renaming_machine_options() -> None:
    parser = _template_parser()
    help_text = parser.format_help()

    assert "Створює шаблон доказу фізичного тестування NVDA" in help_text
    assert "ZIP-файл точного кандидата релізу Autosport" in help_text
    assert "новий JSON-файл шаблону фізичного тестування NVDA" in help_text
    assert "канонічний 40-символьний Git SHA" in help_text
    assert "64-символьний SHA-256 ZIP-файлу релізу" in help_text
    assert "ZIP_РЕЛІЗУ" in help_text
    assert "JSON_ШАБЛОН" in help_text
    assert "GIT_SHA" in help_text
    assert "SHA256_ZIP" in help_text
    assert "--release-zip" in help_text
    assert "--expected-source-sha" in help_text
    assert "--expected-package-sha256" in help_text
    assert "--output" in help_text

    args = parser.parse_args(
        [
            "--release-zip",
            "release.zip",
            "--expected-source-sha",
            "a" * 40,
            "--expected-package-sha256",
            "b" * 64,
            "--output",
            "nvda-evidence.json",
        ]
    )
    assert args.release_zip == Path("release.zip")
    assert args.expected_source_sha == "a" * 40
    assert args.expected_package_sha256 == "b" * 64
    assert args.output == Path("nvda-evidence.json")


def test_nvda_verify_help_is_ukrainian_first_without_renaming_machine_options() -> None:
    parser = _verify_parser()
    help_text = parser.format_help()

    assert "Перевіряє заповнений людиною доказ NVDA" in help_text
    assert "JSON-файл доказу фізичного тестування NVDA, заповнений людиною" in help_text
    assert "необов'язковий JSON-файл машинного звіту перевірки" in help_text
    assert "ZIP_РЕЛІЗУ" in help_text
    assert "JSON_ДОКАЗ" in help_text
    assert "JSON_ЗВІТ" in help_text
    assert "GIT_SHA" in help_text
    assert "SHA256_ZIP" in help_text
    assert "--release-zip" in help_text
    assert "--evidence" in help_text
    assert "--expected-source-sha" in help_text
    assert "--expected-package-sha256" in help_text
    assert "--output" in help_text

    args = parser.parse_args(
        [
            "--release-zip",
            "release.zip",
            "--evidence",
            "nvda-evidence.json",
            "--expected-source-sha",
            "a" * 40,
            "--expected-package-sha256",
            "b" * 64,
            "--output",
            "nvda-validation.json",
        ]
    )
    assert args.release_zip == Path("release.zip")
    assert args.evidence == Path("nvda-evidence.json")
    assert args.expected_source_sha == "a" * 40
    assert args.expected_package_sha256 == "b" * 64
    assert args.output == Path("nvda-validation.json")
