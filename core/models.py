"""Данные диалога: что бот знает о собеседнике и в каком месте сценария находится.

Состояние отделено от канала намеренно: один и тот же Lead одинаково живёт
в консоли, в Telegram и в Авито — меняется только адаптер.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum

from core.profile import Profile


class Stage(str, Enum):
    """Этап диалога. Значения строковые, чтобы состояние сериализовалось как есть."""

    GREETING = "greeting"
    ASKING = "asking"              # собираем профайл
    CONFIRMING = "confirming"      # профайл собран, показали кандидату на проверку
    REGISTRATION = "registration"  # профайл подтверждён, ждём подтверждения регистрации
    DONE = "done"                  # передан координатору
    REJECTED = "rejected"          # отсеян жёстким правилом
    ESCALATED = "escalated"        # ушёл человеку досрочно


class Temperature(str, Enum):
    HOT = "горячий"
    WARM = "тёплый"
    COLD = "холодный"


@dataclass
class Lead:
    """Всё, что собрано о кандидате, плюс служебные отметки."""

    dialog_id: str
    stage: Stage = Stage.GREETING
    profile: Profile = field(default_factory=Profile)
    temperature: Temperature = Temperature.COLD
    score: int = 0
    flags: list[str] = field(default_factory=list)
    reject_reason: str = ""
    questions_asked: list[str] = field(default_factory=list)  # что спрашивал сам кандидат
    reminder_due_at: datetime | None = None
    reminder_sent: bool = False
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    @property
    def answers(self) -> dict[str, str]:
        """Собранные поля. Живут в профайле — здесь только удобный доступ."""
        return self.profile.values

    def set(self, key: str, value: str, derived: bool = False, note: str = "") -> bool:
        changed = self.profile.fill(key, value, derived=derived, note=note)
        if changed:
            self.updated_at = datetime.now()
            if derived and note and note not in self.flags:
                self.flags.append(note)
        return changed

    def schedule_reminder(self, hours: int, now: datetime | None = None) -> None:
        self.reminder_due_at = (now or datetime.now()) + timedelta(hours=hours)
        self.reminder_sent = False

    def reminder_is_due(self, now: datetime) -> bool:
        return (
            self.stage is Stage.REGISTRATION
            and self.reminder_due_at is not None
            and not self.reminder_sent
            and now >= self.reminder_due_at
        )

    def to_row(self) -> dict:
        """Плоская строка для выгрузки в таблицу или CRM."""
        row = {
            "dialog_id": self.dialog_id,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M"),
            "stage": self.stage.value,
            "temperature": self.temperature.value,
            "score": self.score,
            **self.answers,
            "profile_percent": self.profile.percent,
            "profile_confirmed": "да" if self.profile.confirmed else "нет",
            # Менеджер должен видеть, какие поля посчитаны, а не названы:
            # в выгрузке догадка иначе неотличима от факта.
            "derived_fields": "; ".join(sorted(self.profile.derived)),
            "questions_asked": "; ".join(self.questions_asked),
            "flags": "; ".join(self.flags),
            "reject_reason": self.reject_reason,
        }
        return row


@dataclass
class Reply:
    """Ответ бота: текст плюс пометки для менеджера и логов."""

    text: str
    handoff: bool = False          # диалог надо показать человеку
    finished: bool = False         # сценарий закончен
    source: str = "rules"          # rules | knowledge | llm — чем сгенерирован ответ

    def as_dict(self) -> dict:
        return asdict(self)
