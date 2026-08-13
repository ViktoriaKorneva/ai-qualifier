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
        # Промт живёт в конфиге клиента. Его наличие и включает режим, в котором
        # реплики формулирует модель: код по-прежнему решает, ЧТО сказать,
        # модель — КАК. Нет промта или нет ключа — работают заготовки конфига,
        # и диалог идёт ровно так же, только суше.
        self.prompt = config.get("prompt", {})
        self.prompt_template = self.prompt.get("system", "")
        self.history_limit = int(self.prompt.get("history_limit", 12))

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
        """Обработать сообщение человека и запомнить обмен репликами.

        История нужна модели, чтобы разговор был разговором. Она живёт в памяти
        сессии: на диск не пишется и в логи не попадает.
        """
        reply = self._handle(lead, text, now)
        lead.history.append({"role": "user", "text": text})
        lead.history.append({"role": "assistant", "text": reply.text})
        if len(lead.history) > self.history_limit * 2:
            lead.history = lead.history[-self.history_limit * 2:]
        return reply

    def _handle(self, lead: Lead, text: str, now: datetime | None = None) -> Reply:
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
            return Reply(self._with_return(lead, answer.text), source=answer.source)

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
        if self._ready_to_propose(lead):
            return self._to_proposal(lead, notice, user_text=text)

        if lead.profile.is_complete():
            return self._to_confirmation(lead, notice)

        focus = lead.profile.next_focus(Profile.DISCOVERY) or lead.profile.next_focus()
        return Reply(self._say(
            lead,
            self._join(notice, self._ask_next(lead)),
            goal=self._goal_text(focus),
            mandatory=self._mandatory(
                notice,
                "Идёт выявление. Задай ровно один вопрос — тот, что указан в «что нужно "
                "узнать следующим». Ничего не предлагай и цену не называй.",
            ),
            user_text=text,
        ))

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

    def _with_return(self, lead: Lead, answer: str) -> str:
        """Ответ на вопрос плюс возврат к разговору.

        Когда реплику формулирует модель, возврат уже внутри ответа — ей это
        предписано указанием. Приклеить сюда ещё и заготовленный вопрос значит
        задать два вопроса подряд: свой и её.
        """
        if self.prompt_template and getattr(self.llm, "available", False):
            return answer
        return self._join(answer, self._ask_next(lead))

    def _ready_to_propose(self, lead: Lead) -> bool:
        """Пора ли предлагать. Порог задаёт конфиг, а не количество ответов.

        Это то место, где решение было неправильным: предложение после класса
        и предметов читается как «купи» — человек ещё не понял, кто мы и чем
        поможем. Список `requires` держит порог там, где у агента уже есть
        что сказать по существу: уровень и цель.
        """
        if not self.proposal or lead.offer:
            return False
        requires = self.proposal.get("requires")
        if not requires:
            return not self._pending(lead, Profile.DISCOVERY)
        return all(
            lead.profile.has(key) or key in lead.profile.deferred for key in requires
        )

    @staticmethod
    def _mandatory(notice: str, instruction: str, instead: str = "") -> str:
        """Сработавшее правило идёт первым и обязательно к произнесению.

        `instead` заменяет обычную инструкцию, когда правило само по себе уже
        делает её работу: честная оценка ситуации — это и есть «показать, что
        понял», и пересказывать то же ещё раз не нужно.
        """
        if not notice:
            return instruction
        return (
            "СНАЧАЛА скажи вот это — своими словами, одним абзацем, ничего "
            "не смягчая и не обещая результата. Не повторяй формулировку "
            "дословно и не пересказывай одну и ту же мысль дважды:\n"
            f"{notice}\n\nПотом: {instead or instruction}"
        )

    def _to_proposal(self, lead: Lead, notice: str = "", user_text: str = "") -> Reply:
        """Предварительный подбор: что подходит и один уточняющий вопрос.

        Две презентации разведены по уроку 17-02: здесь короткий подбор,
        детали — после того как человек проявил интерес. Смысл в том, чтобы
        дать пользу раньше, чем закончится анкета: до предложения он не знает,
        ради чего отвечает.
        """
        lead.stage = Stage.PROPOSING
        # Подобранная программа считается всегда — она нужна администратору
        # в карточке. Но человеку мы предлагаем не курс, а диагностику:
        # прайс на третьей реплике даёт отторжение, а не продажу.
        lead.offer = rules.pick_offer(lead.profile.values, self.proposal.get("rules", []))

        step = self._proposal_step(lead)
        fallback = self._join(notice, self._offer_text(lead), step)
        text = self._say(
            lead,
            fallback,
            goal=self._goal_text(lead.awaiting) if step else "",
            mandatory=self._mandatory(
                notice,
                "Ключевое собрано. СНАЧАЛА одной-двумя фразами верни человеку его "
                "ситуацию своими словами — что ты понял про класс, предметы, уровень "
                "и срок. ПОТОМ предложи бесплатную диагностику и скажи, что она ему "
                "даёт. Программу не описывай и цену не называй — про них не спрашивали. "
                + ("В конце задай один вопрос по пункту «что нужно узнать следующим»."
                   if step else "Вопросов не задавай."),
                instead=(
                    "коротко предложи бесплатную диагностику как способ проверить "
                    "цель. Ситуацию отдельно не пересказывай — она уже сказана выше. "
                    "Программу не описывай и цену не называй. "
                    + ("В конце задай один вопрос по пункту «что нужно узнать "
                       "следующим»." if step else "Вопросов не задавай.")
                ),
            ),
            user_text=user_text,
            topic=self.proposal.get("show_entry", ""),
        )
        if not step:
            return self._to_confirmation(lead, text)
        return Reply(text)

    def _offer_text(self, lead: Lead) -> str:
        """Заготовка предложения: вступление из конфига плюс запись про диагностику.

        Текст пишет заказчик, а не мы. Описание подобранной программы сюда
        намеренно не попадает: до вопроса о курсе человеку нужен следующий шаг,
        а не витрина.
        """
        intro = self.proposal.get("intro", "").format(**self._fill(lead)).strip()
        body = self.knowledge.by_id(self.proposal.get("show_entry", ""))
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
            return Reply(self._with_return(lead, answer.text), source=answer.source)

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
        return Reply(self._say(
            lead,
            self._join(notice, step),
            goal=self._goal_text(lead.awaiting),
            mandatory=self._mandatory(
                notice,
                "Человек уже видел предложение. Задай один вопрос по пункту «что нужно "
                "узнать следующим». Заново предлагать диагностику не нужно.",
            ),
            user_text=text,
        ))

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

        # Правила не распознали реплику. Это не значит, что человек написал
        # ерунду: «формат ещё не решила» — нормальный ответ, которого просто
        # не было в списке слов. Спрашиваем модель, но решение принимаем сами.
        return self._confirm_by_meaning(lead, text)

    def _confirm_by_meaning(self, lead: Lead, text: str) -> Reply:
        """Разобрать смысл реплики на проверке собранного.

        Модель отвечает одним словом из закрытого списка — всё, что не из
        списка, отбрасывается, и работает прежнее «не поняла». Свободный
        текст модели здесь не разбирается никогда.
        """
        verdict = None
        if getattr(self.llm, "available", False):
            verdict = self.llm.classify(
                "Человеку показали собранные о нём данные и спросили, всё ли верно. "
                "Определи, что он ответил.\n"
                "подтверждает — согласен, данные верны, можно передавать;\n"
                "правит — сообщает другое значение какого-то поля;\n"
                "не_определился — говорит, что по какому-то пункту ещё не решил "
                "или решит позже;\n"
                "другое — что-то ещё.",
                text,
                ["подтверждает", "правит", "не_определился", "другое"],
            )

        if verdict == "подтверждает" and lead.profile.is_complete():
            lead.profile.confirmed = True
            return self._finish_form(lead)

        if verdict == "не_определился":
            return self._handle_undecided(lead, text)

        fallback = self.confirm.get("reask", "Что именно поправить?")
        return Reply(self._say(
            lead,
            fallback,
            mandatory=(
                "Человек ответил не «да» и не поправкой. Ответь коротко и по-человечески, "
                "не повторяя одну и ту же фразу: уточни, что именно поменять, либо "
                "предложи подтвердить, если менять нечего. Данные заново не перечисляй."
            ),
            user_text=text,
        ))

    def _handle_undecided(self, lead: Lead, text: str) -> Reply:
        """Человек ещё не решил по какому-то пункту.

        Раньше такого исхода не существовало: поле было либо заполнено, либо
        не понято. Между ними есть третье — «пока не знаю», и это нормальный
        ответ, а не сбой. Значение при этом стирается: оставить в карточке
        решение, от которого человек отказался, хуже пустого поля.
        """
        optional = [key for key in lead.profile.order if not lead.profile.is_required(key)]
        labels = [self._label(key) for key in optional]
        key = None
        if optional and getattr(self.llm, "available", False):
            chosen = self.llm.classify(
                "Человек говорит, что по одному из пунктов ещё не определился. "
                "Определи, о каком пункте речь.",
                text,
                [label.lower() for label in labels],
            )
            if chosen:
                key = optional[[label.lower() for label in labels].index(chosen)]

        if key:
            lead.profile.clear(key)
            lead.profile.defer(key)
            flag = f"клиент не определился с полем «{self._label(key)}» — обсудить при звонке"
            if flag not in lead.flags:
                lead.flags.append(flag)

        fallback = self._join(
            self.confirm.get("undecided", "Хорошо, обсудим это с администратором."),
            self._summary(lead, with_intro=False),
        )
        return Reply(self._say(
            lead,
            fallback,
            mandatory=(
                "Человек сказал, что по одному из пунктов ещё не определился. Согласись, "
                "что решать это сейчас не нужно — администратор обсудит при звонке. "
                "Не уговаривай и не переспрашивай про этот пункт. Одной фразой спроси, "
                "верно ли всё остальное. Данные заново не перечисляй."
            ),
            user_text=text,
        ))

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

        # Промт клиента задаёт голос и границы — тогда даже готовый ответ базы
        # проговаривается человеческим языком, а не вставляется цитатой.
        # Факты при этом остаются ровно те, что написал заказчик.
        if self.prompt_template and getattr(self.llm, "available", False):
            # Фокус берём по текущему этапу: на выявлении нельзя возвращать
            # человека к вопросу о телефоне, до него ещё не дошли.
            spoken = self._say(
                lead,
                answer or "",
                goal=self._goal_text(self._current_question(lead)["key"]),
                mandatory=(
                    "Человек задал вопрос. Ответь на него по справке — коротко и по "
                    "существу, ничего не добавляя от себя. Если в справке ответа нет, "
                    "честно скажи, что уточнишь у коллег. Про цену отвечай прямо "
                    "цифрами, если спросили. Потом верни разговор к тому, что нужно "
                    "узнать следующим."
                ),
                user_text=text,
                topic=text,
            )
            if spoken:
                return Reply(spoken, source=f"knowledge:{entry_id}" if answer else "llm")

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

    # ------------------------------------------------------------------ #
    # Речь: код решает, что сказать, модель — как
    # ------------------------------------------------------------------ #

    def _say(
        self,
        lead: Lead,
        fallback: str,
        *,
        goal: str = "",
        mandatory: str = "",
        user_text: str = "",
        topic: str = "",
    ) -> str:
        """Реплика от модели поверх заготовки кода. Заготовка — это и есть смысл.

        Модель никогда не решает, что происходит дальше: ей приходит уже
        принятое решение — какое поле нужно, какое правило сработало, какой
        раздел базы относится к теме. Если модели нет или она молчит,
        человек получает заготовку из конфига и разговор не ломается.
        """
        if not self.prompt_template or not getattr(self.llm, "available", False):
            return fallback

        facts, role_brief = self.knowledge.context_for(topic or user_text or goal)
        stages = self.prompt.get("stages", {})
        system = self.prompt_template.format(
            client=self.client_name,
            business=self.config["client"].get("business", ""),
            profile=self._profile_text(lead),
            next_goal=goal or "всё нужное собрано — новых вопросов не задавай",
            stage=stages.get(lead.stage.value, lead.stage.value),
            knowledge=f"{role_brief}\n\n{facts}".strip(),
            mandatory=mandatory or "нет",
        )
        text = (self.llm.compose(system, lead.history[-self.history_limit:], user_text) or "").strip()
        if not text:
            return fallback

        # Промт — это просьба, а не гарантия: модель обходит запрет мягкими
        # формулировками вроде «времени достаточно, чтобы многое изменить».
        # Обещание результата — то, чего этот продукт не делает никогда,
        # поэтому проверяет его код, а не текст промта.
        hit = self._forbidden_hit(text)
        if hit:
            flag = f"ответ модели отклонён фильтром обещаний: «{hit}»"
            if flag not in lead.flags:
                lead.flags.append(flag)
            return fallback
        return text

    def _forbidden_hit(self, text: str) -> str:
        low = text.lower()
        for phrase in self.prompt.get("forbidden", []):
            if str(phrase).lower() in low:
                return str(phrase)
        return ""

    def _profile_text(self, lead: Lead) -> str:
        """Состояние профайла словами — так, как его должна видеть модель.

        Посчитанное помечается прямо здесь: модель не должна ссылаться
        на вычисленное значение как на слова человека.
        """
        lines = []
        for key in lead.profile.order:
            value = lead.profile.values.get(key)
            if not value:
                lines.append(f"- {self._label(key)}: не знаем")
            elif lead.profile.is_derived(key):
                lines.append(
                    f"- {self._label(key)}: {value} "
                    f"(посчитано нами, человек этого не называл)"
                )
            else:
                lines.append(f"- {self._label(key)}: {value} (сказал сам)")
        return "\n".join(lines)

    def _goal_text(self, key: str | None) -> str:
        if not key:
            return ""
        question = self._question(key)
        return f"{self._label(key)}. Пример формулировки: «{question['ask']}»"

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

        # На выявлении спрашиваем только поля выявления. Без этого агент
        # посреди разговора о предметах просит телефон: контакт формально
        # обязателен, и без фильтра по фазе он всплывает первым же.
        key = None
        if lead.stage is Stage.ASKING:
            key = lead.profile.next_focus(Profile.DISCOVERY)
        key = key or lead.profile.next_focus() or self.questions[-1]["key"]
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
