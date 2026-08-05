"""Каналы общения.

Канал обязан уметь ровно три вещи: отдать входящее сообщение, отправить ответ
и сообщить текущее время (его подменяют в демо, чтобы показать напоминание
через 3 часа, не ожидая три часа).

Сейчас реализована консоль. Telegram и Авито добавляются как ещё два класса
с теми же тремя методами — ядро и конфиг при этом не меняются.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta


class Channel(ABC):
    @abstractmethod
    def receive(self) -> str | None: ...

    @abstractmethod
    def send(self, text: str) -> None: ...

    def now(self) -> datetime:
        return datetime.now()


class ConsoleChannel(Channel):
    """Консольный чат. Поддерживает служебные команды демонстрации."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def receive(self) -> str | None:
        try:
            text = input("\033[36mкандидат ›\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            return None

        # Перемотка времени: показать напоминание, не ожидая три часа.
        if text.startswith("/time"):
            parts = text.split()
            hours = float(parts[1].rstrip("hч")) if len(parts) > 1 else 3.0
            self.offset += timedelta(hours=hours)
            print(f"\033[90m[время сдвинуто на +{hours:g} ч]\033[0m")
            return ""
        if text in ("/quit", "/exit"):
            return None
        return text

    def send(self, text: str) -> None:
        print(f"\033[32mбот ›\033[0m {text}\n")

    def now(self) -> datetime:
        return datetime.now() + self.offset
