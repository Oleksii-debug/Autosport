from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .localization import text

SurfacePhase = Literal["v1-active", "visible-disabled", "presentation-only"]


@dataclass(frozen=True, slots=True)
class WindowsSurfaceSpec:
    key: str
    title_uk: str
    purpose_uk: str
    primary_task_uk: str
    controls_uk: str
    focus_entry_uk: str
    focus_exit_uk: str
    accessibility_uk: str
    transient_states_uk: str
    confirmation_uk: str
    authority_uk: str
    persistence_uk: str
    phase: SurfacePhase
    target_widget: str | None
    blocked_reason_uk: str | None = None


SURFACES: Final[tuple[WindowsSurfaceSpec, ...]] = (
    WindowsSurfaceSpec(
        "home_dashboard", "Головна / Огляд",
        "Стартова точка продукту з безпечним переходом до доступних робочих поверхонь.",
        "Перевірити режим, стан робочої області та перейти до потрібної функції.",
        "Навігатор екранів, стан запуску, вибір стратегії.",
        "F2 переводить фокус до навігатора; Tab рухається вперед у стандартному порядку.",
        "Tab переходить до першого доступного робочого контролу; Shift+Tab повертає до навігатора.",
        "Нативні Tk/Windows ролі; стабільні automation id для навігатора, стану, деталей і кнопки переходу.",
        "Порожній стан не вигадує даних; помилки запуску та recovery лишаються видимими у канонічному статусі.",
        "Навігація не виконує економічних дій і не потребує підтвердження.",
        "Лише presentation/navigation; економічна істина залишається в доменних моделях.",
        "Останній вибраний екран зберігається окремо від економічного стану.",
        "v1-active", "strategy",
    ),
    WindowsSurfaceSpec(
        "market_mirror", "Market Mirror / Живі події",
        "Огляд поточного read-only ринкового знімка без перетворення UI на джерело котирувальної істини.",
        "Вибрати режим спостереження, оновити знімок і перевірити його якість.",
        "Режим живого спостереження, оновлення, статус і список котирувань.",
        "Перехід відкриває контроль режиму живого спостереження.",
        "F7 переводить до котирувань; F2 повертає до навігатора.",
        "Read-only котирування мають нативні list semantics; дії мають стабільні імена й automation id.",
        "Порожній/завантажувальний/error/stale стан показується текстом; UI не підміняє freshness/provenance.",
        "Оновлення є read-only запитом; жодного money-moving підтвердження тут немає.",
        "UI показує отриманий observation state; quote freshness/provenance визначає домен.",
        "Вибір shell-екрана зберігається; ринковий стан живе за власними правилами.",
        "v1-active", "live_mode",
    ),
    WindowsSurfaceSpec(
        "research_agents", "Дослідження / Агенти",
        "Керування канонічною стратегією та типізованим дослідницьким планом без обходу research law.",
        "Вибрати стратегію, за потреби прив’язати перевірений research plan.",
        "Стратегія, статус стратегії, кнопка плану дослідження.",
        "Перехід фокусує вибір стратегії або план дослідження.",
        "Tab проходить канонічні research controls; F2 повертає до shell-навігатора.",
        "Combobox/Button нативні; user-facing labels українські; семантичний payload не походить із тексту UI.",
        "Відсутній/invalid/busy plan відображається явно; exploratory UI не стає promotion authority.",
        "Вибір файлу можна скасувати без зміни попередньої валідної конфігурації.",
        "ResearchProtocol/strategy truth визначають доменні типи та frozen evidence, не текст shell.",
        "Вибраний shell-екран зберігається; research identity має власну доменну persistence.",
        "v1-active", "strategy",
    ),
    WindowsSurfaceSpec(
        "opportunities", "Можливості",
        "Майбутня поверхня Opportunity/OpportunitySet для порівняння кандидатів на рівні портфеля.",
        "Оглянути причини недоступності до активації канонічного opportunity runtime.",
        "Лише інформаційний стан; економічні кнопки вимкнені.",
        "Фокус лишається на shell-навігаторі та деталях стану.",
        "F2 повертає до навігатора; Ctrl+Alt+Left/Right рухає між екранами.",
        "Disabled стан має текстову причину; відсутні візуально-єдині критичні дії.",
        "Blocked/empty є очікуваним станом до активації доменного runtime.",
        "Немає підтвердження, бо ця поверхня ще не виконує дій.",
        "UI не розраховує opportunity, stake або expected value.",
        "Shell selection зберігається; доменних даних цей екран не створює.",
        "visible-disabled", None, "Opportunity/portfolio planning runtime ще не активований у V1.",
    ),
    WindowsSurfaceSpec(
        "portfolio", "Портфель",
        "Огляд завершених портфельних доказів без перерахунку грошей у presentation layer.",
        "Перевірити ROI, сценарні межі та exact/approximate truth останнього завершеного replay.",
        "Read-only оцінювання та портфельні рядки.",
        "Перехід фокусує список оцінювання.",
        "F8 переводить до оцінювання; F2 повертає до навігатора.",
        "Нативний Listbox; стабільне accessible name; значення походять лише з ui_model/domain result.",
        "Порожній стан просить спочатку завершити paper replay; partial/error не маскується.",
        "Read-only перегляд не потребує підтвердження.",
        "Portfolio arithmetic та worst/best semantics належать доменному evidence, не shell.",
        "Shell selection зберігається окремо від run/evidence persistence.",
        "v1-active", "evaluation",
    ),
    WindowsSurfaceSpec(
        "paper_bank", "Паперовий банк",
        "Read-only огляд віртуального банку та зарезервованої паперової ставки.",
        "Перевірити balance/committed stake і recovery boundary перед новим replay.",
        "Read-only банк та пов’язані паперові квитки.",
        "Перехід фокусує Windows read-only bank summary, якщо вона вже створена.",
        "Tab переходить далі; F6 переходить до квитків; F2 повертає до shell.",
        "Readonly Entry на Windows має стабільні name/description/automation id.",
        "Pending/recovery-required/quarantined стани показуються текстом без вигаданого balance.",
        "Shell нічого не списує та не підтверджує.",
        "Decimal money authority належить PaperBook/session, а не widget text.",
        "Shell selection зберігається; economic ledger має окрему durability/recovery.",
        "v1-active", "bank_summary",
    ),
    WindowsSurfaceSpec(
        "tickets_positions", "Квитки / Позиції",
        "Read-only список паперових квитків та їх завершальних станів.",
        "Перевірити відкриті/settled paper tickets і перейти до evidence.",
        "Список квитків; без real-money кнопок.",
        "Перехід фокусує список квитків.",
        "F6 переводить до квитків; F2 повертає до shell.",
        "Нативний Listbox зі стабільним accessible name та automation id.",
        "Empty/replay-running/recovery-blocked стани явно відображаються.",
        "Жодного real-money підтвердження або bookmaker submit.",
        "Settlement/ticket truth належить ledger/domain model.",
        "Shell selection зберігається; tickets persistence визначається economic ledger.",
        "v1-active", "tickets",
    ),
    WindowsSurfaceSpec(
        "evaluation_learning", "Оцінювання / Навчання",
        "Перегляд доказів paper replay; promotion-grade learning лишається під scientific governance.",
        "Перевірити завершений evaluation bundle та обмеження узагальнення.",
        "Read-only evaluation list та execution log.",
        "Перехід фокусує evaluation surface.",
        "F8 переводить до evaluation; F2 повертає до shell.",
        "Нативні read-only semantics; UI не конвертує exploratory result у promotion verdict.",
        "Порожній/error/no-terminal-result стани відображаються явно.",
        "Read-only перегляд не потребує підтвердження.",
        "Promotion/evaluation authority належить frozen protocol/evidence, не shell.",
        "Shell selection зберігається; research evidence має власну versioned persistence.",
        "v1-active", "evaluation",
    ),
    WindowsSurfaceSpec(
        "bookmakers_accounts", "Букмекери / Акаунти",
        "Майбутня capability/read-only account surface; real execution залишається вимкненим.",
        "Побачити, що capability ще не активована, замість фальшивого account/execution success.",
        "Лише truthful disabled state.",
        "Фокус лишається на shell-навігаторі та деталях.",
        "F2 повертає до shell; між екранами працює Ctrl+Alt+Left/Right.",
        "Disabled state має текстову причину й не містить прихованої money-moving дії.",
        "Missing provider/config/auth показується як blocked, не як zero balance чи success.",
        "Немає submit/confirm, доки execution program не активований і не кваліфікований.",
        "Bookmaker/account/execution truth не існує в presentation layer.",
        "Shell selection зберігається; credentials тут не зберігаються.",
        "visible-disabled", None, "Bookmaker capability/account runtime та supervised execution ще не активовані.",
    ),
    WindowsSurfaceSpec(
        "history_results", "Історія / Результати",
        "Перегляд текстового журналу та завершених paper-result evidence без редагування історії.",
        "Перевірити причинний журнал останніх операцій і результати.",
        "Read-only лог та пов’язані result/evaluation rows.",
        "Перехід фокусує журнал.",
        "Tab/F2 повертають до інших контрольованих поверхонь.",
        "Text widget має accessible name/description; історія не редагується як domain ledger.",
        "No-result/error/recovery rows не приховуються.",
        "Read-only перегляд не потребує підтвердження.",
        "Durable audit/result truth належить evidence/ledger; log є presentation.",
        "Shell selection зберігається; audit/evidence persistence окрема.",
        "v1-active", "log",
    ),
    WindowsSurfaceSpec(
        "settings", "Налаштування",
        "Майбутня єдина поверхня конфігурації з явною readiness/validation моделлю.",
        "Побачити, що повний settings editor ще не активований, і використовувати наявні перевірені V1 controls.",
        "Інформаційний disabled state; наявні strategy/live controls залишаються на V1 surface.",
        "Фокус лишається на shell-навігаторі та деталях.",
        "F2 повертає до shell.",
        "Неактивні конфігурації не маскуються під editable controls.",
        "Invalid/missing configuration має fail-closed поведінку в канонічних entrypoints.",
        "Немає Apply до появи typed settings contract.",
        "Environment/domain configuration не визначається текстом UI.",
        "Shell selection зберігається; secrets не записуються shell persistence.",
        "visible-disabled", None, "Єдиний typed settings editor ще не реалізований; V1 controls залишаються на робочих поверхнях.",
    ),
    WindowsSurfaceSpec(
        "diagnostics_recovery", "Діагностика / Відновлення",
        "Безпечний доступ до recovery та діагностичного стану без обходу fail-closed boundaries.",
        "Запустити відновлення робочої області або перевірити діагностичний статус.",
        "Кнопка recovery, status/log; machine audits залишаються окремими entrypoint modes.",
        "Перехід фокусує кнопку відновлення.",
        "Control+Shift+R запускає recovery; F2 повертає до shell.",
        "Button/status мають українські accessible labels; recovery лишається off Tk/UIA thread.",
        "Busy/error/rejected/blocked стани показуються явно; unresolved workspace лишається blocked.",
        "Recovery має власні domain checks; shell не підтверджує success до terminal result.",
        "Recovery/session authority належить recovery_worker/session, не navigator.",
        "Shell selection зберігається; recovery durable state має окрему канонічну persistence.",
        "v1-active", "repair_button",
    ),
    WindowsSurfaceSpec(
        "help_about", "Довідка / Про програму",
        "Пояснення режиму V1, keyboard navigation і меж truth без marketing-overclaim.",
        "Прочитати клавіші, поточні обмеження та межі paper-only доказу.",
        "Shell details; без зовнішніх irreversible actions.",
        "Фокус залишається на shell details.",
        "F2 фокусує навігатор; Ctrl+Alt+Left/Right переходить між екранами.",
        "Текст доступний через нативний Listbox/details surface.",
        "Якщо додатковий help content відсутній, основні truth boundaries усе одно показуються.",
        "Немає підтвердження.",
        "Help text не є runtime/economic authority і не змінює V1 flags.",
        "Shell selection зберігається.",
        "presentation-only", None,
    ),
)

DEFAULT_SURFACE_KEY: Final[str] = SURFACES[0].key
SURFACE_BY_KEY: Final[dict[str, WindowsSurfaceSpec]] = {surface.key: surface for surface in SURFACES}
SHELL_STATE_FILENAME: Final[str] = "windows-shell-state-v1.json"


def shell_state_path(workspace: str | Path) -> Path:
    return Path(workspace) / SHELL_STATE_FILENAME


def load_surface_selection(workspace: str | Path) -> str:
    """Read presentation-only shell state; corruption fails soft to Home."""
    path = shell_state_path(workspace)
    try:
        raw = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError):
        return DEFAULT_SURFACE_KEY
    key = raw.get("surface_key") if isinstance(raw, dict) else None
    return key if isinstance(key, str) and key in SURFACE_BY_KEY else DEFAULT_SURFACE_KEY


def save_surface_selection(workspace: str | Path, surface_key: str) -> bool:
    """Atomically persist only UI navigation state; never raise into domain flow."""
    if surface_key not in SURFACE_BY_KEY:
        return False
    path = shell_state_path(workspace)
    try:
        atomic_write_json(path, {"version": 1, "surface_key": surface_key})
    except (OSError, UnicodeError, TypeError, ValueError):
        return False
    return True


def surface_detail_lines(surface: WindowsSurfaceSpec) -> tuple[str, ...]:
    phase = {
        "v1-active": text("ui.windows.surface.phase.active"),
        "visible-disabled": text("ui.windows.surface.phase.disabled"),
        "presentation-only": text("ui.windows.surface.phase.presentation"),
    }[surface.phase]
    lines = (
        phase,
        f"{text('ui.windows.surface.detail.purpose')}: {surface.purpose_uk}",
        f"{text('ui.windows.surface.detail.primary_task')}: {surface.primary_task_uk}",
        f"{text('ui.windows.surface.detail.controls')}: {surface.controls_uk}",
        f"{text('ui.windows.surface.detail.focus_entry')}: {surface.focus_entry_uk}",
        f"{text('ui.windows.surface.detail.focus_exit')}: {surface.focus_exit_uk}",
        f"{text('ui.windows.surface.detail.accessibility')}: {surface.accessibility_uk}",
        f"{text('ui.windows.surface.detail.states')}: {surface.transient_states_uk}",
        f"{text('ui.windows.surface.detail.confirmation')}: {surface.confirmation_uk}",
        f"{text('ui.windows.surface.detail.truth_boundary')}: {surface.authority_uk}",
        f"{text('ui.windows.surface.detail.restart')}: {surface.persistence_uk}",
    )
    if surface.blocked_reason_uk:
        return lines + (
            f"{text('ui.windows.surface.detail.blocked_reason')}: {surface.blocked_reason_uk}",
        )
    return lines
