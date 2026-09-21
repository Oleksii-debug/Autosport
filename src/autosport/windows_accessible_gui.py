from __future__ import annotations

from tkinter import ttk

import tk_uia

from .windows_gui import WindowsAutosportApp


WINDOWS_OPERATIONAL_STATUS_AUTOMATION_ID = 206
OPERATIONAL_STATUS_ACCESSIBLE_NAME_UK = "Операційний стан Автоспорт"
OPERATIONAL_STATUS_ACCESSIBLE_DESCRIPTION_UK = (
    "Лише для читання: поточний стан перевірки набору даних, паперового повтору, "
    "відновлення та живого спостереження. Доступне переходом Tab."
)


class AccessibleWindowsAutosportApp(WindowsAutosportApp):
    """Windows shell with one stable UIA Value surface for operational state.

    The base application keeps ``self.status`` as the only operational-status
    authority. This layer changes only its Windows presentation from a passive
    label to a readonly, focusable Entry so external UIA clients can retrieve the
    current value through ValuePattern without duplicating or translating state.
    """

    def _build(self) -> None:
        super()._build()
        frame = next(iter(self.winfo_children()), None)
        if frame is None:
            raise RuntimeError("Windows GUI root frame is missing")

        status_variable = str(self.status)
        status_label = None
        for child in frame.winfo_children():
            try:
                if (
                    "textvariable" in child.keys()
                    and str(child.cget("textvariable")) == status_variable
                ):
                    status_label = child
                    break
            except Exception:
                continue
        if status_label is None:
            raise RuntimeError("Operational status label is missing")

        siblings = frame.winfo_children()
        status_index = siblings.index(status_label)
        before = siblings[status_index + 1] if status_index + 1 < len(siblings) else None
        status_label.destroy()

        self.operational_status = ttk.Entry(
            frame,
            textvariable=self.status,
            state="readonly",
            takefocus=True,
        )
        if before is None:
            self.operational_status.pack(fill="x", pady=(0, 10))
        else:
            self.operational_status.pack(fill="x", pady=(0, 10), before=before)

    def _configure_accessibility(self) -> None:
        super()._configure_accessibility()
        tk_uia.set_acc_name(
            self.operational_status,
            OPERATIONAL_STATUS_ACCESSIBLE_NAME_UK,
        )
        tk_uia.set_acc_description(
            self.operational_status,
            OPERATIONAL_STATUS_ACCESSIBLE_DESCRIPTION_UK,
        )
        tk_uia.set_automation_id(
            self.operational_status,
            WINDOWS_OPERATIONAL_STATUS_AUTOMATION_ID,
        )


def main() -> int:
    AccessibleWindowsAutosportApp().mainloop()
    return 0
