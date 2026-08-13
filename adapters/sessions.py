"""Сессии диалогов в памяти процесса.

Хранилище намеренно нестойкое: публичная ссылка плюс свободный ввод — это
готовая база персональных данных, которую никто не заказывал. Поэтому диалог
живёт в памяти, умирает по таймауту и не попадает ни в файл, ни в лог.

Перезапуск процесса теряет все сессии. Для демонстрации это правильное
поведение, для продакшена — то, что заменяется хранилищем и договором.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from core.models import Lead


@dataclass
class Session:
    lead: Lead
    messages: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    touched_at: datetime = field(default_factory=datetime.now)


class SessionStore:
    """Словарь сессий с потолком по количеству, времени жизни и длине диалога.

    Все три ограничения — про публичный стенд без авторизации: без них одна
    вкладка, оставленная открытой на ночь, и один скрипт в цикле одинаково
    съедают память процесса.
    """

    def __init__(self, ttl_minutes: int = 60, max_sessions: int = 500, max_messages: int = 60):
        self.ttl = timedelta(minutes=ttl_minutes)
        self.max_sessions = max_sessions
        self.max_messages = max_messages
        self._sessions: dict[str, Session] = {}

    def create(self, now: datetime | None = None) -> tuple[str, Session]:
        now = now or datetime.now()
        self.purge(now)

        # Потолок сессий: выбрасываем самую давнюю, а не отказываем новому
        # посетителю. На демонстрации отказ выглядит как поломка стенда.
        while len(self._sessions) >= self.max_sessions:
            oldest = min(self._sessions, key=lambda key: self._sessions[key].touched_at)
            self._sessions.pop(oldest, None)

        session_id = secrets.token_urlsafe(16)
        session = Session(lead=Lead(dialog_id=session_id[:8]), created_at=now, touched_at=now)
        self._sessions[session_id] = session
        return session_id, session

    def get(self, session_id: str, now: datetime | None = None) -> Session | None:
        now = now or datetime.now()
        self.purge(now)
        session = self._sessions.get(session_id)
        if session is not None:
            session.touched_at = now
        return session

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def purge(self, now: datetime | None = None) -> int:
        now = now or datetime.now()
        stale = [key for key, item in self._sessions.items() if now - item.touched_at > self.ttl]
        for key in stale:
            del self._sessions[key]
        return len(stale)

    def __len__(self) -> int:
        return len(self._sessions)
