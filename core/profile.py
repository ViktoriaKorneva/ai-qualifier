"""Профайл: что бот знает о кандидате, независимо от порядка вопросов.

Это замена счётчику вопросов, и разница принципиальная. Счётчик помнит, на каком
шаге мы стоим, и теряет всё, сказанное не в свою очередь: человек написал
«Светлана, 23, Заречный, 89001234567» — бот забрал имя и пошёл спрашивать
возраст, который ему только что назвали.

Профайл помнит не шаг, а данные. Поле заполняется из любой реплики, в любой
момент, и повторно не спрашивается. Порядок вопросов остаётся, но перестаёт
быть жёстким.

Метод из урока 17-01 УИИ, архитектура с ветками и блоками:
профайл — это информационное поле, а не список вопросов.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Profile:
    """Поля кандидата плюс отметки о том, что отложено и что подтверждено."""

    order: list[str] = field(default_factory=list)   # ключи в порядке по умолчанию
    values: dict[str, str] = field(default_factory=dict)
    derived: set[str] = field(default_factory=set)   # поля, посчитанные нами, а не названные
    notes: dict[str, str] = field(default_factory=dict)  # чем именно рискованно выведенное
    deferred: set[str] = field(default_factory=set)  # «отвечу позже»
    attempts: dict[str, int] = field(default_factory=dict)  # сколько раз переспрашивали
    confirmed: bool = False

    # ------------------------------------------------------------------ #
    # Заполнение
    # ------------------------------------------------------------------ #

    def fill(self, key: str, value: str, derived: bool = False, note: str = "") -> bool:
        """Записать значение. True — если что-то реально изменилось.

        Изменение сбрасывает подтверждение: профайл, который поправили после
        «всё верно», обязан быть подтверждён заново.

        `derived` означает, что значение вычислено нами, а не названо кандидатом.
        Такое поле остаётся отмеченным, пока человек не назовёт его сам: как
        только он поправит возраст словами, отметка снимается.
        """
        if self.values.get(key) == value and self.is_derived(key) == derived:
            return False
        self.values[key] = value
        if derived:
            self.derived.add(key)
            if note:
                self.notes[key] = note
        else:
            self.derived.discard(key)
            self.notes.pop(key, None)
        self.deferred.discard(key)
        self.confirmed = False
        return True

    def is_derived(self, key: str) -> bool:
        return key in self.derived

    def defer(self, key: str) -> None:
        """Кандидат уклонился — запоминаем и возвращаемся к полю позже."""
        if key not in self.values:
            self.deferred.add(key)

    def failed_attempt(self, key: str, limit: int = 2) -> bool:
        """Отметить неудачную попытку. True — пора отложить поле и идти дальше.

        Первый раз переспрашиваем на месте: чаще всего человек просто отшутился
        или ошибся. Второй раз откладываем — иначе диалог зацикливается на одном
        поле и кандидат уходит.
        """
        self.attempts[key] = self.attempts.get(key, 0) + 1
        if self.attempts[key] >= limit:
            self.defer(key)
            return True
        return False

    def has(self, key: str) -> bool:
        return bool(self.values.get(key))

    # ------------------------------------------------------------------ #
    # Что спрашивать дальше
    # ------------------------------------------------------------------ #

    def next_focus(self) -> str | None:
        """Следующее поле для вопроса. Отложенные уходят в конец очереди."""
        for key in self.order:
            if not self.has(key) and key not in self.deferred:
                return key
        for key in self.order:            # круг второй: возвращаемся к отложенным
            if not self.has(key):
                return key
        return None

    def missing(self) -> list[str]:
        return [key for key in self.order if not self.has(key)]

    def is_complete(self) -> bool:
        return not self.missing()

    # ------------------------------------------------------------------ #
    # Прогресс — машинное состояние для интерфейса и логов
    # ------------------------------------------------------------------ #

    @property
    def total(self) -> int:
        return len(self.order)

    @property
    def completed(self) -> int:
        return sum(1 for key in self.order if self.has(key))

    @property
    def percent(self) -> int:
        return round(self.completed / self.total * 100) if self.total else 0

    def state(self) -> dict:
        """Снимок состояния. Формат из урока: сколько собрано и куда смотрим.

        `sources` отвечает на вопрос, который иначе задать некому: что кандидат
        сказал сам, а что мы за него посчитали.
        """
        return {
            "extracted": {key: self.values.get(key) for key in self.order},
            "sources": {
                key: ("выведено" if self.is_derived(key) else "сказано")
                for key in self.order
                if self.has(key)
            },
            "notes": dict(self.notes),
            "progress": {
                "completed_fields": self.completed,
                "total_fields": self.total,
                "percent": self.percent,
            },
            "deferred": sorted(self.deferred),
            "confirmed": self.confirmed,
            "next_focus": self.next_focus(),
        }
