#!/usr/bin/env python3
"""Запуск демо: диалог квалификации в консоли.

    python run.py                        # клиент по умолчанию — «Смена»
    python run.py --client clients/smena.yaml

Служебные команды в диалоге:
    /time 3     сдвинуть время на 3 часа (показать напоминание)
    /quit       выйти и сохранить лида
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adapters.channel import ConsoleChannel
from adapters.storage import CsvStorage
from core.dialog import QualifierDialog
from core.llm import LLMClient
from core.models import Lead


def load_client_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> int:
    # Windows-консоль по умолчанию не в utf-8 — без этого кириллица бьётся
    # и на вводе (кандидат), и на выводе (бот).
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="AI-квалификатор входящих откликов")
    parser.add_argument("--client", default="clients/smena.yaml", help="конфиг клиента")
    parser.add_argument("--leads", default="data/leads.csv", help="куда писать лидов")
    args = parser.parse_args()

    config = load_client_config(Path(args.client))
    llm = LLMClient()
    dialog = QualifierDialog(config, llm)
    channel = ConsoleChannel()
    storage = CsvStorage(args.leads)

    lead = Lead(dialog_id=uuid.uuid4().hex[:8])

    mode = f"модель {llm.model}" if llm.available else "без модели, только правила и база знаний"
    print(f"\033[90m— демо «{config['client']['name']}» · {mode} · /time 3 · /quit —\033[0m\n")

    channel.send(dialog.start(lead).text)

    while True:
        text = channel.receive()
        if text is None:
            break

        if text:
            reply = dialog.handle(lead, text, now=channel.now())
            channel.send(reply.text)
            if reply.source.startswith(("knowledge", "llm", "fallback")):
                print(f"\033[90m[источник ответа: {reply.source}]\033[0m")
            if reply.finished:
                break

        # После каждого хода проверяем, не пора ли напомнить о регистрации.
        reminder = dialog.tick(lead, channel.now())
        if reminder:
            channel.send(reminder.text)

    storage.save(lead)
    print(
        f"\033[90m— лид {lead.dialog_id}: {lead.temperature.value}, балл {lead.score}, "
        f"этап «{lead.stage.value}» → {args.leads} —\033[0m"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
