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

Правила, нарушать нельзя:
1. Отвечай ТОЛЬКО фактами из блока «БАЗА ЗНАНИЙ». Ничего не додумывай.
2. Если ответа в базе нет — так и скажи и предложи передать вопрос координатору.
3. Никаких обещаний по деньгам, срокам и условиям сверх написанного в базе.
4. Два-три предложения, спокойный человеческий тон, без канцелярита и эмодзи.
5. В конце верни кандидата к анкете одним коротким вопросом.

БАЗА ЗНАНИЙ:
{knowledge}"""


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

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def answer(self, question: str, knowledge: str, client_name: str) -> str | None:
        """Ответить на свободный вопрос по базе знаний. None — модель недоступна."""
        if not self.available:
            return None

        payload = {
            "model": self.model,
            "temperature": 0.3,
            "max_tokens": 300,
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT.format(client=client_name, knowledge=knowledge),
                },
                {"role": "user", "content": question},
            ],
        }
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
            return response.json()["choices"][0]["message"]["content"].strip()
        except (requests.RequestException, KeyError, ValueError):
            # Молча падать нельзя, но и ронять диалог из-за модели — тоже:
            # кандидат получит ответ по правилам, менеджер увидит флаг в логе.
            return None
