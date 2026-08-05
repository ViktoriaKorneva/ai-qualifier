"""Разбор ответов кандидата и жёсткие правила отсева.

Здесь нет ни одного текста и ни одного порога — всё приходит из конфига клиента.
Код отвечает на вопрос «как разобрать и проверить», конфиг — «что именно».
"""

from __future__ import annotations

import re

# Слова, за которыми обычно нет ответа на вопрос, а есть уклонение.
EVASIVE = ("не скажу", "зачем", "а вам зачем", "секрет", "потом", "не важно", "неважно")


def parse_answer(kind: str, text: str, options: list[str] | None = None) -> str | None:
    """Привести ответ к каноничному виду. None означает «не разобрала»."""
    text = text.strip()
    if not text:
        return None

    if kind == "text":
        return _parse_name(text)
    if kind == "age":
        return _parse_age(text)
    if kind == "phone":
        return _parse_phone(text)
    if kind == "choice":
        return _parse_choice(text, options or [])
    return text


def _parse_name(text: str) -> str | None:
    """Имя. Люди пишут «Меня зовут Аня», «аня», «Аня 22 года» — берём слово."""
    if any(word in text.lower() for word in EVASIVE):
        return None

    cleaned = re.sub(r"(меня зовут|зовут|это|я)\s+", " ", text, flags=re.IGNORECASE)
    words = re.findall(r"[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z-]{1,}", cleaned)
    if not words:
        return None
    return words[0].capitalize()


def _parse_age(text: str) -> str | None:
    """Возраст. Принимаем «23», «мне 23», «23 года»; отсекаем годы рождения и мусор."""
    numbers = [int(n) for n in re.findall(r"\d{1,4}", text)]
    for number in numbers:
        if 10 <= number <= 99:
            return str(number)
        # «2003» — назвали год рождения, а не возраст.
        if 1900 <= number <= 2020:
            return str(2026 - number)
    return None


def _parse_phone(text: str) -> str | None:
    """Телефон. Приводим к +7XXXXXXXXXX, проверяем длину и код страны."""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    return None


def _parse_choice(text: str, options: list[str]) -> str | None:
    """Выбор из списка. Сравниваем по началу слова: «в заречном» → «заречный»."""
    low = text.lower()
    for option in options:
        stem = option[: max(4, len(option) - 2)]
        if stem in low:
            return option
    return None


def check_rules(field_key: str, value: str, rules: list[dict]) -> tuple[str | None, str]:
    """Проверить жёсткие правила по полю.

    Возвращает (action, message): action — reject / flag / None.
    """
    for rule in rules:
        if rule["field"] != field_key:
            continue
        if not _condition_met(rule, value):
            continue
        return rule["action"], rule.get("message", "")
    return None, ""


def _condition_met(rule: dict, value: str) -> bool:
    try:
        number = float(value)
    except ValueError:
        return False

    condition, threshold = rule["condition"], float(rule["value"])
    if condition == "less_than":
        return number < threshold
    if condition == "greater_than":
        return number > threshold
    if condition == "equals":
        return number == threshold
    return False


def looks_like_question(text: str) -> bool:
    """Кандидат задал вопрос вместо ответа на анкету.

    Это ключевое отличие от опросника: человек в любой момент может спросить
    про оплату или график, и бот обязан ответить, а не повторять свой вопрос.
    """
    low = text.strip().lower()
    if "?" in low:
        return True
    starters = (
        "а сколько", "сколько", "какой", "какая", "какие", "как ", "когда", "где",
        "что нужно", "нужно ли", "можно ли", "а если", "правда ли", "а что",
    )
    return low.startswith(starters)


def score_lead(answers: dict, confirmed: bool, config: dict) -> tuple[str, int]:
    """Посчитать балл и температуру лида.

    Балл прозрачный и объяснимый — менеджер должен понимать, почему «горячий».
    """
    score = len([v for v in answers.values() if v])  # каждое заполненное поле = 1
    if confirmed:
        score += 2

    scoring = config["scoring"]
    if score >= scoring["hot"]["min_score"] and confirmed:
        return "горячий", score
    if score >= scoring["warm"]["min_score"]:
        return "тёплый", score
    return "холодный", score
