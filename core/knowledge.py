"""База знаний клиента: ответы на типовые вопросы без обращения к модели.

Порядок работы принципиальный: сначала ищем готовый ответ по ключевым словам,
и только если не нашли — идём в LLM. Так дешевле, быстрее и, главное,
предсказуемее: на вопрос про ставку клиент получит ровно ту цифру,
которую сам указал, а не пересказ модели.
"""

from __future__ import annotations


class KnowledgeBase:
    def __init__(self, entries: list[dict]):
        self.entries = entries

    # Совпадение целой фразой весит больше одиночного слова: вопрос
    # «когда деньги придут» должен попасть в сроки выплаты, а не в размер ставки,
    # хотя слово «деньги» есть в обеих записях.
    PHRASE_WEIGHT = 2
    WORD_WEIGHT = 1

    def find(self, question: str) -> tuple[str | None, str]:
        """Найти ответ. Возвращает (ответ или None, id записи)."""
        low = question.lower()
        best_entry, best_score = None, 0

        for entry in self.entries:
            score = 0
            for keyword in entry["keywords"]:
                if keyword not in low:
                    continue
                score += self.PHRASE_WEIGHT if " " in keyword else self.WORD_WEIGHT
            if score > best_score:
                best_entry, best_score = entry, score

        if best_entry is None:
            return None, ""
        return best_entry["answer"].strip(), best_entry["id"]

    def as_prompt_context(self) -> str:
        """Вся база одним куском — контекст для модели, когда точного ответа нет."""
        return "\n\n".join(
            f"[{entry['id']}] {entry['answer'].strip()}" for entry in self.entries
        )
