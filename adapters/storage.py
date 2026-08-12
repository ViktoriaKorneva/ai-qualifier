"""Куда складывать собранных лидов.

Интерфейс один, реализаций может быть много: CSV сейчас, Google Sheets или CRM
у клиента потом. Ядро про хранилище ничего не знает — вызывает save() и всё.
"""

from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from pathlib import Path

from core.models import Lead

FIELDS = [
    "dialog_id", "created_at", "stage", "temperature", "score",
    "name", "age", "phone", "district",
    "profile_percent", "profile_confirmed",
    "questions_asked", "flags", "reject_reason",
]


class Storage(ABC):
    @abstractmethod
    def save(self, lead: Lead) -> None: ...


class CsvStorage(Storage):
    """CSV с фиксированными колонками — открывается в Excel и Google Таблицах.

    Осознанное ограничение учебного прогона: у заказчика на этом месте будет
    Google Sheets API, но интерфейс останется тем же.
    """

    def __init__(self, path: str | Path = "data/leads.csv"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, lead: Lead) -> None:
        row = lead.to_row()
        is_new = not self.path.exists()

        with self.path.open("a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            writer.writerow({key: row.get(key, "") for key in FIELDS})


class MemoryStorage(Storage):
    """Хранилище для тестов: ничего не пишет на диск."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def save(self, lead: Lead) -> None:
        self.rows.append(lead.to_row())
