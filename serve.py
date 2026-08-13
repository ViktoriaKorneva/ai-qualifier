#!/usr/bin/env python3
"""Запуск веб-демо.

    python serve.py                 # http://127.0.0.1:8000
    QUALIFIER_CONFIG=clients/smena.yaml python serve.py

Порт берётся из переменной окружения PORT: платформы задают его сами,
а вписанный в код номер рано или поздно расходится с тем, что слушает балансер.
"""

from __future__ import annotations

import os

import uvicorn

from adapters.web import create_app

app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        # Журнал доступа пишет метод и путь. Идентификатор сессии и тексты
        # диалога передаются телом запроса и в журнал не попадают.
        access_log=True,
    )
