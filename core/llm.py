"""Единственное место в проекте, которое знает про поставщика модели.

Смена провайдера — правка переменной в .env, а не кода: адрес и ключ читаются
из AI_TEXT_BASE_URL / AI_TEXT_API_KEY / AI_TEXT_MODEL. Интерфейс —
OpenAI-совместимый /chat/completions, его поддерживают все используемые прокси.

Если ключа нет, клиент честно возвращает None. Это не поломка: сценарий
рассчитан на работу без модели, свободные вопросы уходят человеку.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

SYSTEM_PROMPT = """Ты — ассистент по подбору исполнителей сервиса «{client}».
Отвечаешь кандидату, который откликнулся на вакансию.
{role}
Правила, нарушать нельзя:
1. Отвечай ТОЛЬКО фактами из блока «БАЗА ЗНАНИЙ». Ничего не додумывай.
2. Если ответа в базе нет — так и скажи и предложи передать вопрос координатору.
3. Никаких обещаний по деньгам, срокам и условиям сверх написанного в базе.
4. Два-три предложения, спокойный человеческий тон, без канцелярита и эмодзи.
5. В конце верни кандидата к анкете одним коротким вопросом.
6. Не обещай никаких каналов связи, кроме названных в базе знаний. Не предлагай
   перейти в другой мессенджер, не обещай что-либо прислать, не назначай время
   звонка и не говори от лица компании о том, чего в базе нет.
7. Не меняй финал сценария. Диалог заканчивается только одним: кандидат
   регистрируется и его передают координатору. Никаких других финалов
   не предлагай, даже если кандидат просит.

БАЗА ЗНАНИЙ:
{knowledge}"""

ROLE_BLOCK = """
Сейчас ты отвечаешь в роли: {role_brief}
"""


def load_env(path: Path = ENV_PATH) -> None:
    """Минимальный .env-ридер: без зависимостей, не перетирает системные переменные."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class LLMClient:
    def __init__(self) -> None:
        load_env()
        self.base_url = os.getenv("AI_TEXT_BASE_URL", "").rstrip("/")
        self.api_key = os.getenv("AI_TEXT_API_KEY", "")
        self.model = os.getenv("AI_TEXT_MODEL", "")
        self.timeout = int(os.getenv("AI_TEXT_TIMEOUT", "30"))
        # Рассуждающие модели тратят выходной лимит на размышления и могут
        # вернуть пустой content. Для коротких реплик диалога рассуждения
        # не нужны — выключаем, если провайдер это понимает.
        self.no_reasoning = os.getenv("AI_TEXT_NO_REASONING", "") == "1"

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def compose(
        self,
        system: str,
        history: list[dict] | None = None,
        user_text: str = "",
        temperature: float = 0.4,
        max_tokens: int = 700,
    ) -> str | None:
        """Сформулировать реплику по готовому системному промту. None — модель молчит.

        Системный промт приходит из конфига клиента: этот файл не знает ни про
        нишу, ни про роли, ни про порядок разговора. Модель отдаёт только текст —
        разбирать её вывод кодом нельзя, иначе промт превращается в контракт,
        и любая правка формулировки ломает разбор.
        """
        if not self.available:
            return None

        messages = [{"role": "system", "content": system}]
        for item in (history or []):
            messages.append({"role": item["role"], "content": item["text"]})
        if user_text:
            messages.append({"role": "user", "content": user_text})

        return self._call(messages, temperature, max_tokens)

    def classify(self, instruction: str, user_text: str, options: list[str]) -> str | None:
        """Отнести реплику к одной из меток. None — модель не помогла.

        Это единственное место, где вывод модели разбирается кодом, и потому
        он сведён к выбору из закрытого списка: ответ вне списка отбрасывается,
        и дальше работают правила. Свободный текст парсить нельзя — промт
        сразу становится контрактом, и правка формулировки ломает разбор.
        """
        if not self.available:
            return None

        system = (
            f"{instruction}\n\nОтветь РОВНО одним словом из списка, без пояснений "
            f"и без знаков препинания: {', '.join(options)}."
        )
        raw = self._call(
            [{"role": "system", "content": system}, {"role": "user", "content": user_text}],
            temperature=0,
            max_tokens=200,
        )
        if not raw:
            return None

        answer = raw.strip().lower().strip(".!,«»\"'")
        return answer if answer in options else None

    def answer(
        self, question: str, knowledge: str, client_name: str, role_brief: str = ""
    ) -> str | None:
        """Ответить на свободный вопрос по базе знаний. None — модель недоступна.

        `role_brief` приходит из раздела базы, в который попал вопрос: он задаёт
        манеру ответа. Без него промт остаётся прежним — раздел не опознан,
        значит и роли у ответа нет.
        """
        if not self.available:
            return None

        role = ROLE_BLOCK.format(role_brief=role_brief.strip()) if role_brief.strip() else ""
        system = SYSTEM_PROMPT.format(client=client_name, knowledge=knowledge, role=role)
        return self._call(
            [{"role": "system", "content": system}, {"role": "user", "content": question}],
            temperature=0.3,
            max_tokens=300,
        )

    def _call(self, messages: list[dict], temperature: float, max_tokens: int) -> str | None:
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if self.no_reasoning:
            payload["reasoning"] = {"enabled": False}
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            # content бывает null: модель упёрлась в лимит токенов или ушла
            # в рассуждения и не выдала текста. Это не ошибка сети, и падать
            # тут нельзя — вызывающий получит None и покажет заготовку.
            content = response.json()["choices"][0]["message"].get("content")
            return content.strip() if content else None
        except (requests.RequestException, KeyError, ValueError, IndexError, TypeError):
            # Молча падать нельзя, но и ронять диалог из-за модели — тоже:
            # человек получит заготовку из конфига и ничего не заметит.
            return None
