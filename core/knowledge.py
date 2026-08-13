"""База знаний клиента: ответы на типовые вопросы без обращения к модели.

Порядок работы принципиальный: сначала ищем готовый ответ по ключевым словам,
и только если не нашли — идём в LLM. Так дешевле, быстрее и, главное,
предсказуемее: на вопрос про ставку клиент получит ровно ту цифру,
которую сам указал, а не пересказ модели.

База разбита на разделы, и раздел — это **адресация роли**, а не оглавление.
У каждого раздела своя роль со своей манерой: про график отвечает координатор
смен, про договор и налоги — специалист по оформлению. Вопрос сначала
маршрутизируется в раздел, и модель получает только его — вместе с описанием
роли, от лица которой отвечает.

Метод из урока 17-02 УИИ: там разделы базы («психологические аспекты»,
«речевые проблемы») кормили соответствующих агентов промта. Агентская схема
бесполезна, если база собрана как каталог товара: специалисту неоткуда
взять ответ.
"""

from __future__ import annotations


class KnowledgeBase:
    """Разделы базы, маршрутизация вопроса и сборка контекста для модели."""

    # Совпадение целой фразой весит больше одиночного слова: вопрос
    # «когда деньги придут» должен попасть в сроки выплаты, а не в размер ставки,
    # хотя слово «деньги» есть в обеих записях.
    PHRASE_WEIGHT = 2
    WORD_WEIGHT = 1

    def __init__(self, sections: list[dict]):
        # Принимаем оба формата: разделы с ролями и плоский список записей.
        # Плоский — наследство первой версии конфига; ломать чужие конфиги
        # ради нашей же перестройки незачем.
        if sections and "entries" in sections[0]:
            self.sections = sections
        else:
            self.sections = [{"section": "general", "role": "", "role_brief": "", "entries": sections}]

        self.entries: list[dict] = []
        self._section_of: dict[str, dict] = {}
        for section in self.sections:
            for entry in section.get("entries", []):
                self.entries.append(entry)
                self._section_of[entry["id"]] = section

    # ------------------------------------------------------------------ #
    # Поиск готового ответа
    # ------------------------------------------------------------------ #

    def find(self, question: str) -> tuple[str | None, str]:
        """Найти готовый ответ. Возвращает (ответ или None, id записи)."""
        best_entry, best_score = None, 0
        for entry in self.entries:
            score = self._score(entry, question)
            if score > best_score:
                best_entry, best_score = entry, score

        if best_entry is None:
            return None, ""
        return best_entry["answer"].strip(), best_entry["id"]

    def _score(self, entry: dict, question: str) -> int:
        low = question.lower()
        score = 0
        for keyword in entry["keywords"]:
            if keyword not in low:
                continue
            score += self.PHRASE_WEIGHT if " " in keyword else self.WORD_WEIGHT
        return score

    # ------------------------------------------------------------------ #
    # Маршрутизация в раздел
    # ------------------------------------------------------------------ #

    def route(self, question: str) -> dict | None:
        """Определить, какому разделу принадлежит вопрос. None — не опознали."""
        best_section, best_score = None, 0
        for section in self.sections:
            score = sum(self._score(entry, question) for entry in section.get("entries", []))
            if score > best_score:
                best_section, best_score = section, score
        return best_section

    def context_for(self, question: str) -> tuple[str, str]:
        """Контекст для модели: (факты, описание роли).

        Отдаём только тот раздел, куда попал вопрос. Если не опознали —
        отдаём всё: лучше лишние токены, чем ответ мимо темы.
        """
        section = self.route(question)
        if section is None:
            return self._facts(self.entries), ""
        return self._facts(section.get("entries", [])), self._brief(section)

    @staticmethod
    def _facts(entries: list[dict]) -> str:
        return "\n\n".join(f"[{entry['id']}] {entry['answer'].strip()}" for entry in entries)

    @staticmethod
    def _brief(section: dict) -> str:
        role = (section.get("role") or "").strip()
        brief = (section.get("role_brief") or "").strip()
        if not role and not brief:
            return ""
        return f"{role}. {brief}".strip(". ").strip()

    def as_prompt_context(self) -> str:
        """Вся база одним куском. Оставлено для выгрузок и отладки."""
        return self._facts(self.entries)
