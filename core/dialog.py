"""Ядро: машина состояний диалога квалификации.

Это единственный файл, который решает, что происходит дальше. Он не знает
ни про канал (консоль, Telegram, Авито), ни про хранилище, ни про конкретного
клиента — всё приходит извне. Именно поэтому ядро переносится к другому
заказчику без правок: меняются YAML и адаптеры.
"""

from __future__ import annotations

import re
from datetime import datetime

from core import rules
from core.knowledge import KnowledgeBase
from core.llm import LLMClient
from core.models import Lead, Reply, Stage, Temperature


class QualifierDialog:
    def __init__(self, config: dict, llm: LLMClient | None = None):
        self.config = config
        self.questions = config["questions"]
        self.knowledge = KnowledgeBase(config["knowledge"])
        self.llm = llm if llm is not None else LLMClient()
        self.client_name = config["client"]["name"]

    # ------------------------------------------------------------------ #
    # Точки входа
    # ------------------------------------------------------------------ #

    def start(self, lead: Lead) -> Reply:
        """Первое сообщение: приветствие + первый вопрос анкеты."""
        lead.stage = Stage.ASKING
        greeting = self.config["greeting"].strip()
        return Reply(f"{greeting}\n\n{self._current_question(lead)['ask']}")

    def handle(self, lead: Lead, text: str, now: datetime | None = None) -> Reply:
        """Обработать сообщение кандидата."""
        now = now or datetime.now()
        text = text.strip()

        if lead.stage in (Stage.DONE, Stage.REJECTED, Stage.ESCALATED):
            return Reply("Диалог уже завершён.", finished=True)

        # Тревожные темы уводим человеку немедленно, на любом этапе.
        if self._needs_escalation(text):
            lead.stage = Stage.ESCALATED
            lead.flags.append("эскалация по стоп-слову")
            return Reply(
                self.config["fallback"]["escalate"].format(
                    manager_contact=self.config["client"]["manager_contact"]
                ).strip(),
                handoff=True,
                finished=True,
            )

        if lead.stage is Stage.ASKING:
            return self._handle_asking(lead, text)
        if lead.stage is Stage.REGISTRATION:
            return self._handle_registration(lead, text)
        return Reply("Не поняла, на каком мы шаге. Передаю координатору.", handoff=True)

    def tick(self, lead: Lead, now: datetime) -> Reply | None:
        """Проверить, не пора ли отправить напоминание. None — пока рано.

        Напоминание живёт в ядре, а не в канале: иначе каждый новый канал
        пришлось бы учить одному и тому же таймеру заново.
        """
        if not lead.reminder_is_due(now):
            return None
        lead.reminder_sent = True
        text = self.config["registration"]["reminder_text"].format(**self._fill(lead))
        return Reply(text.strip(), source="rules")

    # ------------------------------------------------------------------ #
    # Этапы
    # ------------------------------------------------------------------ #

    def _handle_asking(self, lead: Lead, text: str) -> Reply:
        question = self._current_question(lead)

        # Кандидат спросил своё вместо ответа — сначала отвечаем ему.
        if rules.looks_like_question(text):
            answer = self._answer_question(lead, text)
            return Reply(
                f"{answer.text}\n\n{question['ask']}",
                source=answer.source,
            )

        value = rules.parse_answer(question["type"], text, question.get("options"))
        if value is None:
            return Reply(question["reask"])

        lead.set(question["key"], value)

        action, message = rules.check_rules(question["key"], value, self.config.get("rules", []))
        if action == "reject":
            lead.stage = Stage.REJECTED
            lead.reject_reason = f"{question['key']}={value}"
            lead.temperature = Temperature.COLD
            return Reply(message.strip(), finished=True)
        if action == "flag":
            lead.flags.append(message)

        lead.question_index += 1
        if lead.question_index < len(self.questions):
            return Reply(self._current_question(lead)["ask"])

        return self._finish_form(lead)

    def _finish_form(self, lead: Lead) -> Reply:
        """Анкета собрана: отправляем инструкцию и ставим таймер напоминания."""
        lead.stage = Stage.REGISTRATION
        registration = self.config["registration"]
        lead.schedule_reminder(registration["reminder_after_hours"])

        temperature, score = rules.score_lead(lead.answers, False, self.config)
        lead.temperature = Temperature(temperature)
        lead.score = score

        return Reply(registration["instruction"].format(**self._fill(lead)).strip())

    def _handle_registration(self, lead: Lead, text: str) -> Reply:
        # Сравниваем по словам, а не по подстрокам: иначе «когда» содержит «да»,
        # и вопрос кандидата засчитывается как подтверждение регистрации.
        words = set(re.findall(r"[а-яёa-z]+", text.lower()))
        confirm_words = set(self.config["registration"]["confirm_words"])

        if words & confirm_words:
            lead.stage = Stage.DONE
            temperature, score = rules.score_lead(lead.answers, True, self.config)
            lead.temperature = Temperature(temperature)
            lead.score = score
            return Reply(
                self.config["handoff"]["message"].format(**self._fill(lead)).strip(),
                handoff=True,
                finished=True,
            )

        # Не подтвердил — скорее всего у него вопрос или затык.
        answer = self._answer_question(lead, text)
        return Reply(
            f"{answer.text}\n\nКак зарегистрируетесь — напишите «готово».",
            source=answer.source,
        )

    # ------------------------------------------------------------------ #
    # Ответы на вопросы кандидата
    # ------------------------------------------------------------------ #

    def _answer_question(self, lead: Lead, text: str) -> Reply:
        """База знаний → модель → честное «не знаю». Именно в этом порядке."""
        lead.questions_asked.append(text)

        answer, entry_id = self.knowledge.find(text)
        if answer:
            return Reply(answer, source=f"knowledge:{entry_id}")

        generated = self.llm.answer(
            text, self.knowledge.as_prompt_context(), self.client_name
        )
        if generated:
            return Reply(generated, source="llm")

        lead.flags.append(f"вопрос без ответа: {text}")
        return Reply(self.config["fallback"]["unknown_question"].strip(), source="fallback")

    # ------------------------------------------------------------------ #
    # Мелочи
    # ------------------------------------------------------------------ #

    def _current_question(self, lead: Lead) -> dict:
        return self.questions[min(lead.question_index, len(self.questions) - 1)]

    def _needs_escalation(self, text: str) -> bool:
        low = text.lower()
        return any(word in low for word in self.config["fallback"]["escalate_triggers"])

    def _fill(self, lead: Lead) -> dict:
        """Подстановки для шаблонов: собранные поля плюс запасные значения."""
        data = {question["key"]: "—" for question in self.questions}
        data.update(lead.answers)
        data["manager_contact"] = self.config["client"]["manager_contact"]
        return data
