from __future__ import annotations

from types import MappingProxyType


PRODUCT_CLI_UK_UA = MappingProxyType(
    {
        "product.cli.description": (
            "Запустити канонічний довговічний PAPER-продукт Autosport. "
            "Облікові дані провайдера залишаються зовнішніми для Autosport і ніколи "
            "не передаються як аргументи командного рядка."
        ),
        "product.cli.help": "Показати цю довідку та завершити роботу.",
        "product.cli.usage_prefix": "використання: ",
        "product.cli.options_heading": "Параметри:",
        "product.cli.workspace.help": (
            "Робочий каталог канонічного продукту. За замовчуванням: .autosport-product."
        ),
        "product.cli.source_factory.help": (
            "Зовнішня фабрика джерела продукту у форматі module:function."
        ),
        "product.cli.bankroll.help": (
            "Початковий PAPER-банкрол; значення не надає повноважень реального виконання."
        ),
        "product.cli.max_cycles.help": (
            "Необов'язкова кількість циклів для обмеженого кваліфікаційного або "
            "контрольованого запуску."
        ),
        "product.cli.poll_seconds.help": (
            "Інтервал між циклами в секундах для безперервного запуску."
        ),
    }
)
