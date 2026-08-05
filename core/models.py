"""Данные диалога: что бот знает о собеседнике и в каком месте сценария находится.

Состояние отделено от канала намеренно: один и тот же Lead одинаково живёт
в консоли, в Telegram и в Авито — меняется только адаптер.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum


class Stage(str, Enum):
    """Этап диалога. Значения строковые, чтобы состояние сериализовалось как есть."""

    GREETING = "greeting"
    ASKING = "asking"              # идём по вопросам анкеты
    REGISTRATION = "registration"  # анкета собрана, ждём подтверждения регистрации
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
    answers: dict[str, str] = field(default_factory=dict)
    question_index: int = 0
    temperature: Temperature = Temperature.COLD
    score: int = 0
    flags: list[str] = field(default_factory=list)
    reject_reason: str = ""
    questions_asked: list[str] = field(default_factory=list)  # что спрашивал сам кандидат
    reminder_due_at: datetime | None = None
    reminder_sent: bool = False
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def set(self, key: str, value: str) -> None:
        self.answers[key] = value
        self.updated_at = datetime.now()

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
