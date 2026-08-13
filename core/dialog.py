"""Ядро: машина состояний диалога квалификации.

Это единственный файл, который решает, что происходит дальше. Он не знает
ни про канал (консоль, Telegram, Авито), ни про хранилище, ни про конкретного
клиента — всё приходит извне. Именно поэтому ядро переносится к другому
заказчику без правок: меняются YAML и адаптеры.

Состояние диалога — не номер вопроса, а профайл (`core/profile.py`). Бот идёт
по полям в порядке из конфига, но забирает данные из любой реплики и не
переспрашивает то, что уже знает.

Этапы: приветствие → сбор профайла → проверка собранного → регистрация → передача.
"""

from __future__ import annotations

import re
from datetime import datetime

from core import rules
from core.knowledge import KnowledgeBase
from core.llm import LLMClient
from core.models import Lead, Reply, Stage, Temperature
from core.profile import Profile


class QualifierDialog:
    def __init__(self, config: dict, llm: LLMClient | None = None):
        self.config = config
        self.questions = config["questions"]
        self.knowledge = KnowledgeBase(config["knowledge"])
        self.llm = llm if llm is not None else LLMClient()
        self.client_name = config["client"]["name"]
        self.confirm = config.get("profile_confirm", {})

    # ------------------------------------------------------------------ #
    # Точки входа
    # ------------------------------------------------------------------ #

    def start(self, lead: Lead) -> Reply:
        """Первое сообщение: приветствие + вопрос по первому пустому полю."""
        self._ensure_profile(lead)
        lead.stage = Stage.ASKING
        greeting = self.config["greeting"].strip()
        return Reply(f"{greeting}\n\n{self._ask_next(lead)}")

    def handle(self, lead: Lead, text: str, now: datetime | None = None) -> Reply:
        """Обработать сообщение кандидата."""
        now = now or datetime.now()
        text = text.strip()
        self._ensure_profile(lead)

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

        if not text:
            return Reply(self._current_question(lead)["reask"])

        if lead.stage is Stage.ASKING:
            return self._handle_asking(lead, text)
        if lead.stage is Stage.CONFIRMING:
            return self._handle_confirming(lead, text)
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
    # Этап: сбор профайла
    # ------------------------------------------------------------------ #

    def _handle_asking(self, lead: Lead, text: str) -> Reply:
        # Кандидат спросил своё вместо ответа — сначала отвечаем ему,
        # потом возвращаемся ровно туда, где прервались.
        if rules.looks_like_question(text):
            answer = self._answer_question(lead, text)
            return Reply(f"{answer.text}\n\n{self._ask_next(lead)}", source=answer.source)

        filled, reject = self._absorb(lead, text)
        if reject:
            return reject

        if not filled:
            # Ничего не разобрали: переспрашиваем на месте, а после второй
            # неудачи откладываем поле и идём дальше, чтобы не зациклиться.
            question = self._current_question(lead)
            if lead.profile.failed_attempt(question["key"]):
                if lead.profile.next_focus() is None:
                    return self._to_confirmation(lead)
                # Все оставшиеся поля отложены — дальше уговаривать бессмысленно,
                # такой диалог полезнее человеку, чем ещё одному кругу вопросов.
                if set(lead.profile.missing()) <= lead.profile.deferred:
                    lead.stage = Stage.ESCALATED
                    lead.flags.append("кандидат не отвечает на вопросы анкеты")
                    return Reply(
                        self.config["fallback"]["escalate"].format(
                            manager_contact=self.config["client"]["manager_contact"]
                        ).strip(),
                        handoff=True,
                        finished=True,
                    )
                return Reply(f"{question['reask']}\n\n{self._ask_next(lead)}")
            return Reply(question["reask"])

        if lead.profile.is_complete():
            return self._to_confirmation(lead)
        return Reply(self._ask_next(lead))

    def _absorb(self, lead: Lead, text: str, overwrite: bool = False) -> tuple[list[str], Reply | None]:
        """Забрать из реплики всё, что удалось опознать.

        Возвращает (какие поля заполнены, ответ-отказ если сработало жёсткое правило).
        `overwrite` разрешает переписывать уже заполненные поля — так работает
        исправление («я перепутал, мне 19») и правка на этапе проверки.
        """
        overwrite = overwrite or rules.looks_like_correction(text)
        filled: list[str] = []

        # Сначала сканируем всю реплику: телефон, район, возраст, имя по маркеру.
        for key, parsed in rules.scan_fields(text, self.questions).items():
            if lead.profile.has(key) and not overwrite:
                continue
            if lead.set(key, parsed.value, derived=parsed.derived, note=parsed.note):
                filled.append(key)

        # Потом добираем активное поле сфокусированным разбором: на прямой вопрос
        # человек отвечает короче и без маркеров — «Аня», «23», «в заречном».
        question = self._current_question(lead)
        key = question["key"]
        if key not in filled and (overwrite or not lead.profile.has(key)):
            parsed = rules.parse_answer(question["type"], text, question.get("options"))
            if parsed is not None and lead.set(
                key, parsed.value, derived=parsed.derived, note=parsed.note
            ):
                filled.append(key)

        # И наконец одинокое слово-имя. Человек отвечает «Валера» на вопрос
        # о возрасте, потому что имя мы у него так и не получили: слово ничем
        # больше быть не может, и терять его глупо.
        if not filled:
            for question in self.questions:
                if question["type"] != "text" or lead.profile.has(question["key"]):
                    continue
                value = rules.parse_lone_name(text)
                if value and lead.set(question["key"], value):
                    filled.append(question["key"])
                break

        # Жёсткие правила проверяем по каждому заполненному полю: возраст мог
        # приехать из общей реплики, а не из ответа на прямой вопрос.
        # Выведенные значения отсев не запускают — решение остаётся за человеком.
        for key in filled:
            action, message = rules.check_rules(
                key,
                lead.profile.values[key],
                self.config.get("rules", []),
                derived=lead.profile.is_derived(key),
            )
            if action == "reject":
                lead.stage = Stage.REJECTED
                lead.reject_reason = f"{key}={lead.profile.values[key]}"
                lead.temperature = Temperature.COLD
                return filled, Reply(message.strip(), finished=True)
            if action == "flag" and message not in lead.flags:
                lead.flags.append(message)

        return filled, None

    # ------------------------------------------------------------------ #
    # Этап: проверка собранного
    # ------------------------------------------------------------------ #

    def _to_confirmation(self, lead: Lead) -> Reply:
        lead.stage = Stage.CONFIRMING
        return Reply(self._summary(lead))

    def _handle_confirming(self, lead: Lead, text: str) -> Reply:
        if rules.looks_like_question(text):
            answer = self._answer_question(lead, text)
            return Reply(f"{answer.text}\n\n{self._summary(lead)}", source=answer.source)

        low = text.lower()
        words = self._words(text)

        # Правки принимаем в первую очередь: «да, только район северный» — это
        # правка, а не подтверждение, и подтверждающее слово не должно её съесть.
        filled, reject = self._absorb(lead, text, overwrite=True)
        if reject:
            return reject
        if filled:
            intro = self.confirm.get("changed", "Поправила. Проверьте ещё раз:")
            return Reply(f"{intro}\n\n{self._summary(lead, with_intro=False)}")

        if words & set(self.confirm.get("confirm_words", [])):
            # Подтверждению неполного профайла не верим — правило из урока.
            if not lead.profile.is_complete():
                missing = ", ".join(self._label(key) for key in lead.profile.missing())
                lead.stage = Stage.ASKING
                template = self.confirm.get("incomplete", "Не хватает: {missing}.")
                return Reply(f"{template.format(missing=missing)}\n\n{self._ask_next(lead)}")
            lead.profile.confirmed = True
            return self._finish_form(lead)

        # «Нет» без уточнения — человек не согласен, но не сказал с чем именно.
        if any(word in low for word in self.confirm.get("reject_words", [])):
            return Reply(self.confirm.get("what_to_fix", "Что именно поправить?"))

        return Reply(self.confirm.get("reask", "Что именно поправить?"))

    def _summary(self, lead: Lead, with_intro: bool = True) -> str:
        """Сводка собранного и вопрос «всё верно?»."""
        lines = [
            f"— {self._label(key)}: {lead.profile.values.get(key, '—')}"
            for key in lead.profile.order
        ]
        parts = []
        if with_intro:
            parts.append(self.confirm.get("summary_intro", "Проверьте, всё ли верно:"))
        parts.append("\n".join(lines))
        parts.append(self.confirm.get("ask", "Всё верно?"))
        return "\n\n".join(parts)

    def _label(self, key: str) -> str:
        return self.confirm.get("labels", {}).get(key, key)

    # ------------------------------------------------------------------ #
    # Этап: регистрация и передача
    # ------------------------------------------------------------------ #

    def _finish_form(self, lead: Lead) -> Reply:
        """Профайл принят: отправляем инструкцию и ставим таймер напоминания."""
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
        words = self._words(text)
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

        # В модель уходит не вся база, а раздел, куда попал вопрос, вместе
        # с ролью этого раздела: про договор и налоги отвечает специалист
        # по оформлению, а не тот же голос, что задаёт вопросы анкеты.
        facts, role_brief = self.knowledge.context_for(text)
        generated = self.llm.answer(text, facts, self.client_name, role_brief)
        if generated:
            return Reply(generated, source="llm")

        lead.flags.append(f"вопрос без ответа: {text}")
        return Reply(self.config["fallback"]["unknown_question"].strip(), source="fallback")

    # ------------------------------------------------------------------ #
    # Мелочи
    # ------------------------------------------------------------------ #

    def _ensure_profile(self, lead: Lead) -> None:
        """Порядок полей задаёт конфиг клиента, а не код."""
        if not lead.profile.order:
            lead.profile = Profile(order=[q["key"] for q in self.questions])

    def _current_question(self, lead: Lead) -> dict:
        """Вопрос по текущему фокусу профайла."""
        key = lead.profile.next_focus() or self.questions[-1]["key"]
        return next(q for q in self.questions if q["key"] == key)

    def _ask_next(self, lead: Lead) -> str:
        return self._current_question(lead)["ask"]

    def _needs_escalation(self, text: str) -> bool:
        low = text.lower()
        return any(word in low for word in self.config["fallback"]["escalate_triggers"])

    @staticmethod
    def _words(text: str) -> set[str]:
        return set(re.findall(r"[а-яёa-z]+", text.lower()))

    def _fill(self, lead: Lead) -> dict:
        """Подстановки для шаблонов: собранные поля плюс запасные значения."""
        data = {question["key"]: "—" for question in self.questions}
        data.update(lead.answers)
        data["manager_contact"] = self.config["client"]["manager_contact"]
        return data
