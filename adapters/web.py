"""HTTP-адаптер: то же ядро, только вход по сети.

Адаптер стоит ровно там же, где консольный канал и хранилище: он переводит
запрос в вызов `QualifierDialog` и обратно. Ядро про HTTP не знает и не должно —
уберите этот файл, и продукт останется работать в консоли.

Идентификатор сессии передаётся телом запроса, а не в пути. Разница
практическая: путь попадает в журнал доступа веб-сервера, тело — нет.
Демонстрация обещает не хранить ничего, и обещание проверяется в том числе
тем, чего нет в логах.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from adapters.sessions import SessionStore
from core.dialog import QualifierDialog
from core.llm import LLMClient
from core.models import Lead, Stage

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "web"

MAX_TEXT = 400          # длиннее человек в чат не пишет, а скрипт пишет что угодно


class StartRequest(BaseModel):
    pass


class MessageRequest(BaseModel):
    # Потолок здесь только против мусора: нормальный длинный ввод принимаем
    # и обрезаем сами. Отказ с 422 на публичном стенде выглядит как поломка,
    # а не как защита.
    session_id: str = Field(min_length=1, max_length=64)
    text: str = Field(default="", max_length=20_000)


class SessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)


def mask_value(value: str) -> str:
    """Спрятать середину значения, оставив узнаваемые края.

    Телефон на публичном стенде показывать целиком незачем: демонстрации
    достаточно доказать, что контакт собран и доехал до карточки.
    """
    if not value:
        return value
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 10:
        return f"+7 {digits[-10:-7]} ***-**-{digits[-2:]}"
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def create_app(config_path: str | Path | None = None, llm=None) -> FastAPI:
    path = Path(config_path or os.getenv("QUALIFIER_CONFIG", "clients/parabola.yaml"))
    if not path.is_absolute():
        path = ROOT / path
    config = yaml.safe_load(path.read_text(encoding="utf-8"))

    dialog = QualifierDialog(config, llm if llm is not None else LLMClient())
    sessions = SessionStore(
        ttl_minutes=int(os.getenv("SESSION_TTL_MINUTES", "60")),
        max_sessions=int(os.getenv("MAX_SESSIONS", "500")),
        max_messages=int(os.getenv("MAX_MESSAGES", "60")),
    )

    demo = config.get("demo", {}) or {}
    masked_fields = set(demo.get("mask_fields", []))
    labels = config.get("profile_confirm", {}).get("labels", {})
    computed_keys = {spec["key"] for spec in config.get("computed", [])}

    app = FastAPI(title=f"AI-квалификатор — {config['client']['name']}", docs_url=None, redoc_url=None)

    # ---------------------------------------------------------------- #
    # Сборка состояния для интерфейса
    # ---------------------------------------------------------------- #

    def show(key: str, value: str) -> str:
        return mask_value(value) if key in masked_fields and value else value

    def mask_reply(text: str, lead: Lead) -> str:
        """Спрятать маскируемые значения и в том, что бот произносит вслух.

        Профайл маскировать мало: телефон приезжает обратно в текстах —
        «администратор перезвонит по номеру …». На демонстрации с проектором
        видно именно реплику, а не колонку.
        """
        for key in masked_fields:
            value = lead.profile.values.get(key)
            if not value:
                continue
            masked = mask_value(value)
            text = text.replace(value, masked)
            digits = re.sub(r"\D", "", value)
            if len(digits) >= 10:                     # та же цифра в другой записи
                for variant in (digits, "8" + digits[-10:]):
                    text = text.replace(variant, masked)
        return text

    def profile_view(lead: Lead) -> list[dict]:
        """Профайл с источником каждого значения.

        `source` — то, ради чего вся эта возня: интерфейс обязан показывать,
        что человек сказал сам, а что мы за него посчитали. Без метки догадка
        в карточке неотличима от факта.
        """
        state = lead.profile.state()
        return [
            {
                "key": key,
                "label": labels.get(key, key),
                "value": show(key, lead.profile.values.get(key) or ""),
                "masked": key in masked_fields and bool(lead.profile.values.get(key)),
                "source": state["sources"].get(key, ""),
                "note": lead.profile.notes.get(key, ""),
                "required": lead.profile.is_required(key),
                "computed": key in computed_keys,
            }
            for key in lead.profile.order
        ]

    def state_view(lead: Lead) -> dict:
        return {
            "stage": lead.stage.value,
            "temperature": lead.temperature.value,
            "score": lead.score,
            "offer": lead.offer,
            "profile": profile_view(lead),
            "progress": lead.profile.state()["progress"],
            "flags": list(lead.flags),
            "finished": lead.stage in (Stage.DONE, Stage.REJECTED, Stage.ESCALATED),
        }

    def handoff_view(lead: Lead) -> dict:
        """Карточка, которая ушла бы администратору.

        Флаги едут сюда целиком и намеренно: в прототипе на платформе пометка
        о вычисленном значении оставалась в профайле и до сводки не доезжала,
        а лид при этом получал статус «горячий». Догадка не пряталась —
        она повышала приоритет.
        """
        return {
            "client": config["client"]["name"],
            "manager_contact": config["client"]["manager_contact"],
            "stage": lead.stage.value,
            "temperature": lead.temperature.value,
            "score": lead.score,
            "offer": lead.offer,
            "fields": [item for item in profile_view(lead) if item["value"]],
            "derived_fields": sorted(lead.profile.derived),
            "flags": list(lead.flags),
            "questions_asked": lead.questions_asked[-10:],
            "reject_reason": lead.reject_reason,
            "note": config.get("handoff", {}).get("manager_note", ""),
        }

    # ---------------------------------------------------------------- #
    # Эндпоинты
    # ---------------------------------------------------------------- #

    @app.get("/health")
    def health() -> dict:
        """Проба для платформы. Ни одного поля из диалогов — только жив ли процесс."""
        return {"status": "ok", "client": config["client"]["name"], "sessions": len(sessions)}

    @app.get("/api/config")
    def get_config() -> dict:
        return {
            "client": config["client"]["name"],
            "business": config["client"].get("business", ""),
            "demo_notice": demo.get("notice", ""),
            "masked_fields": sorted(masked_fields),
            "labels": labels,
        }

    @app.post("/api/session")
    def start_session() -> dict:
        session_id, session = sessions.create()
        reply = dialog.start(session.lead)
        return {"session_id": session_id, "reply": reply.text, "state": state_view(session.lead)}

    @app.post("/api/message")
    def send_message(request: MessageRequest) -> dict:
        session = sessions.get(request.session_id)
        if session is None:
            # Сессия истекла или процесс перезапущен. Для стенда это норма,
            # а не ошибка: интерфейс просто начинает диалог заново.
            raise HTTPException(status_code=404, detail="session_expired")

        session.messages += 1
        if session.messages > sessions.max_messages:
            raise HTTPException(status_code=429, detail="too_many_messages")

        text = (request.text or "").strip()[:MAX_TEXT]
        reply = dialog.handle(session.lead, text)
        return {
            "reply": mask_reply(reply.text, session.lead),
            "source": reply.source,
            "handoff": reply.handoff,
            "state": state_view(session.lead),
        }

    @app.post("/api/state")
    def get_state(request: SessionRequest) -> dict:
        session = sessions.get(request.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session_expired")
        return state_view(session.lead)

    @app.post("/api/handoff")
    def get_handoff(request: SessionRequest) -> dict:
        session = sessions.get(request.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session_expired")
        return handoff_view(session.lead)

    @app.post("/api/reset")
    def reset(request: SessionRequest) -> dict:
        """Забыть диалог по требованию посетителя, не дожидаясь таймаута."""
        sessions.drop(request.session_id)
        return {"status": "dropped"}

    # Статика появляется на следующем этапе; без неё API работает сам по себе.
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR / "static"), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app
