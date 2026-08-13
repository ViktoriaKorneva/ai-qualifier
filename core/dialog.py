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
        # Этап предложения включается наличием секции в конфиге. Её нет —
        # диалог остаётся прежним: собрали профайл, показали, передали.
        self.proposal = config.get("proposal", {})
        self.computed = config.get("computed", [])

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
        if lead.stage is Stage.PROPOSING:
            return self._handle_proposing(lead, text)
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
                # Все поля, которые вообще можно сейчас спросить, отложены —
                # дальше уговаривать бессмысленно, такой диалог полезнее человеку,
                # чем ещё один круг вопросов. Считаем по фазе: контакт спрашивают
                # на закрытии, и его отсутствие на этом шаге ничего не значит.
                pending = self._pending(lead, Profile.DISCOVERY)
                if pending and set(pending) <= lead.profile.deferred:
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

        notice = self._combined_notice(lead)

        # Порог перехода к предложению качественный, а не процентный: собрано
        # ключевое — предлагаем, остальное уточняем по ходу. Проверяется раньше
        # полноты профайла, иначе человек, выложивший всё одной репликой,
        # проскочил бы предложение и получил анкету вместо разговора.
        if self.proposal and not lead.offer and not self._pending(lead, Profile.DISCOVERY):
            return self._to_proposal(lead, notice)

        if lead.profile.is_complete():
            return self._to_confirmation(lead, notice)
        return Reply(self._join(notice, self._ask_next(lead)))

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
            parsed = rules.parse_answer(
                question["type"], text, question.get("options"), question.get("ranges")
            )
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

        # Что можно посчитать из собранного — считаем сразу: срок до экзамена
        # обязан обновиться в ту же секунду, когда человек поправил класс.
        if filled:
            self._recompute(lead)

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
    # Этап: предложение
    # ------------------------------------------------------------------ #

    def _to_proposal(self, lead: Lead, notice: str = "") -> Reply:
        """Предварительный подбор: что подходит и один уточняющий вопрос.

        Две презентации разведены по уроку 17-02: здесь короткий подбор,
        детали — после того как человек проявил интерес. Смысл в том, чтобы
        дать пользу раньше, чем закончится анкета: до предложения он не знает,
        ради чего отвечает.
        """
        lead.stage = Stage.PROPOSING
        lead.offer = rules.pick_offer(lead.profile.values, self.proposal.get("rules", []))

        parts = [notice] if notice else []
        parts.append(self._offer_text(lead))
        step = self._proposal_step(lead)
        if not step:
            return self._to_confirmation(lead, "\n\n".join(parts))
        parts.append(step)
        return Reply("\n\n".join(part for part in parts if part))

    def _offer_text(self, lead: Lead) -> str:
        """Текст предложения = вступление из конфига + запись базы знаний.

        Описание программы пишет заказчик, а не мы: подставляется тот же
        текст, который лежит в базе, — иначе появятся две версии условий,
        и рано или поздно они разойдутся.
        """
        intro = self.proposal.get("intro", "").format(**self._fill(lead)).strip()
        body = self.knowledge.by_id(lead.offer) if lead.offer else ""
        if not body:
            return intro or self.proposal.get("no_match", "").strip()
        return f"{intro}\n\n{body}".strip()

    def _proposal_step(self, lead: Lead) -> str:
        """Следующий вопрос на этапе предложения. Пусто — пора показывать сводку.

        Уточняющих вопросов ограниченное число: агент задуман быстрым, и
        превращать предложение обратно в анкету нельзя. Что не спросили —
        спросит администратор.
        """
        if lead.followups < int(self.proposal.get("max_followups", 3)):
            key = lead.profile.next_focus(Profile.DISCOVERY)
            if key:
                lead.followups += 1
                lead.awaiting = key
                return self._question(key)["ask"]

        key = lead.profile.next_focus(Profile.CLOSING)
        if key:
            lead.awaiting = key
            return self._question(key)["ask"]
        return ""

    def _handle_proposing(self, lead: Lead, text: str) -> Reply:
        if rules.looks_like_question(text):
            answer = self._answer_question(lead, text)
            return Reply(f"{answer.text}\n\n{self._ask_next(lead)}", source=answer.source)

        # «Давайте запишемся» — человек готов раньше, чем мы закончили уточнять.
        # Продолжать расспросы в этот момент — терять уже готового лида.
        ready = bool(self._words(text) & set(self.proposal.get("ready_words", [])))
        if ready:
            lead.followups = int(self.proposal.get("max_followups", 3))

        filled, reject = self._absorb(lead, text)
        if reject:
            return reject

        # Класс могли поправить — тогда и предложение меняется.
        if filled:
            lead.offer = rules.pick_offer(lead.profile.values, self.proposal.get("rules", []))

        notice = self._combined_notice(lead)

        if not filled and not notice and not ready:
            question = self._current_question(lead)
            if not lead.profile.failed_attempt(question["key"]):
                return Reply(question["reask"])
            # Дважды не разобрали — поле откладываем и идём дальше, а не
            # упираемся в него: желательное поле не стоит потерянного лида.

        step = self._proposal_step(lead)
        if not step:
            return self._to_confirmation(lead, notice)
        return Reply(self._join(notice, step))

    # ------------------------------------------------------------------ #
    # Составные правила
    # ------------------------------------------------------------------ #

    def _combined_notice(self, lead: Lead) -> str:
        """Текст сработавшего правила по комбинации полей. Пусто — не сработало.

        Каждое правило говорит один раз: повторять человеку, что его цель под
        вопросом, в каждой реплике — это уже не честность, а давление.
        """
        said: list[str] = []
        for index, rule in enumerate(self.config.get("combined_rules", [])):
            rule_id = rule.get("id", str(index))
            if rule_id in lead.applied_rules:
                continue
            result = rules.check_combined(rule, lead.profile.values)
            if not result:
                continue
            _action, message, flag = result
            lead.applied_rules.add(rule_id)
            if flag and flag not in lead.flags:
                lead.flags.append(flag)
            if message:
                said.append(message)
        return "\n\n".join(said)

    # ------------------------------------------------------------------ #
    # Этап: проверка собранного
    # ------------------------------------------------------------------ #

    def _to_confirmation(self, lead: Lead, notice: str = "") -> Reply:
        lead.stage = Stage.CONFIRMING
        lead.awaiting = ""
        return Reply(self._join(notice, self._summary(lead)))

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
            # Правка могла изменить расклад: поправленный класс меняет срок
            # до экзамена, а с ним и ответ на вопрос, реалистична ли цель.
            # Молча оставить прежнюю оценку — значит соврать по недосмотру.
            notice = self._combined_notice(lead)
            return Reply(self._join(intro, notice, self._summary(lead, with_intro=False)))

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
        """Порядок, обязательность и фазы полей задаёт конфиг клиента, а не код.

        Конфиг, который про обязательность и фазы ничего не говорит, получает
        прежнее поведение: все поля обязательные, фаза одна.
        """
        if lead.profile.order:
            return

        computed = [spec["key"] for spec in self.computed]
        lead.profile = Profile(
            order=[q["key"] for q in self.questions] + computed,
            required={q["key"] for q in self.questions if q.get("required", True)}
            if any("required" in q for q in self.questions)
            else set(),
            phases={
                q["key"]: q.get("phase", Profile.DISCOVERY)
                for q in self.questions
                if q.get("phase")
            },
            computed=set(computed),
        )

    def _recompute(self, lead: Lead) -> None:
        """Пересчитать вычисляемые поля по текущему состоянию профайла."""
        for spec in self.computed:
            parsed = rules.compute_field(spec, lead.profile.values)
            if parsed is not None:
                lead.set(spec["key"], parsed.value, derived=True, note=parsed.note)

    def _pending(self, lead: Lead, phase: str) -> list[str]:
        """Незаполненные обязательные поля указанной фазы."""
        return [key for key in lead.profile.missing() if lead.profile.phase_of(key) == phase]

    def _question(self, key: str) -> dict:
        return next(q for q in self.questions if q["key"] == key)

    def _current_question(self, lead: Lead) -> dict:
        """Вопрос по текущему фокусу профайла.

        На этапе предложения фокус задан явно: там вопросы идут не подряд
        по списку, а по одному между кусками предложения, и разбирать ответ
        нужно именно под заданный вопрос.
        """
        if lead.stage is Stage.PROPOSING and lead.awaiting:
            if not lead.profile.has(lead.awaiting):
                return self._question(lead.awaiting)
        key = lead.profile.next_focus() or self.questions[-1]["key"]
        return self._question(key)

    @staticmethod
    def _join(*parts: str) -> str:
        return "\n\n".join(part.strip() for part in parts if part and part.strip())

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
        data.update({spec["key"]: "—" for spec in self.computed})
        data.update(lead.answers)
        data["manager_contact"] = self.config["client"]["manager_contact"]
        return data
