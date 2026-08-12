#!/usr/bin/env python3
"""Прогон тестовых диалогов из scenarios.yaml.

Запуск:
    python tests/run_tests.py           # итоговая таблица
    python tests/run_tests.py -v        # + полная расшифровка каждого диалога

Модель при прогоне намеренно отключена: тесты проверяют сценарий и правила,
а не формулировки LLM. Иначе они станут недетерминированными и бесполезными.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.storage import MemoryStorage
from core.dialog import QualifierDialog
from core.models import Lead

GREEN, RED, GREY, BOLD, RESET = "\033[32m", "\033[31m", "\033[90m", "\033[1m", "\033[0m"


class OfflineLLM:
    """Заглушка модели: тесты не ходят в сеть и не тратят деньги."""

    available = False
    model = "offline"

    def answer(self, *_args, **_kwargs) -> None:
        return None


def run_scenario(scenario: dict, config: dict, verbose: bool) -> tuple[bool, list[str]]:
    dialog = QualifierDialog(config, OfflineLLM())
    lead = Lead(dialog_id=uuid.uuid4().hex[:8])
    sources: list[str] = []

    asks = [q["ask"] for q in config["questions"]]
    questions_asked = 0

    first = dialog.start(lead)
    questions_asked += count_asks(first.text, asks)
    transcript = [f"{GREY}бот:{RESET} {first.text.splitlines()[0]} …"]

    for message in scenario["messages"]:
        reply = dialog.handle(lead, message)
        questions_asked += count_asks(reply.text, asks)
        sources.append(reply.source)
        transcript.append(f"{GREY}кандидат:{RESET} {message or '(пусто)'}")
        transcript.append(f"{GREY}бот:{RESET} {reply.text.splitlines()[0]}")
        if reply.finished:
            break

    if verbose:
        print("\n".join("   " + line for line in transcript))

    return check(scenario.get("expect", {}), lead, sources, questions_asked)


def count_asks(text: str, asks: list[str]) -> int:
    """Сколько вопросов анкеты бот задал в одном сообщении."""
    return sum(1 for ask in asks if ask in text)


def check(expect: dict, lead: Lead, sources: list[str], questions_asked: int) -> tuple[bool, list[str]]:
    problems: list[str] = []

    if "stage" in expect and lead.stage.value != expect["stage"]:
        problems.append(f"этап {lead.stage.value!r}, ожидали {expect['stage']!r}")

    if "temperature" in expect and lead.temperature.value != expect["temperature"]:
        problems.append(f"температура {lead.temperature.value!r}, ожидали {expect['temperature']!r}")

    for key, value in expect.get("answers", {}).items():
        actual = lead.answers.get(key)
        if actual != value:
            problems.append(f"{key}={actual!r}, ожидали {value!r}")

    if "asked" in expect and len(lead.questions_asked) != expect["asked"]:
        problems.append(f"вопросов кандидата {len(lead.questions_asked)}, ожидали {expect['asked']}")

    for source in expect.get("sources", []):
        if source not in sources:
            problems.append(f"нет ответа из источника {source!r} (были: {', '.join(sorted(set(sources)))})")

    if expect.get("flags_not_empty") and not lead.flags:
        problems.append("ожидали флаг для менеджера, флагов нет")

    if "percent" in expect and lead.profile.percent != expect["percent"]:
        problems.append(f"заполненность {lead.profile.percent}%, ожидали {expect['percent']}%")

    if "confirmed" in expect and lead.profile.confirmed != expect["confirmed"]:
        problems.append(f"подтверждение {lead.profile.confirmed}, ожидали {expect['confirmed']}")

    # Главная проверка профайла: бот не должен спрашивать то, что ему уже
    # сказали. Лишний заданный вопрос анкеты — признак ровно этого.
    if "max_questions" in expect and questions_asked > expect["max_questions"]:
        problems.append(
            f"бот задал {questions_asked} вопросов анкеты, ожидали не больше {expect['max_questions']}"
        )

    return not problems, problems


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    verbose = "-v" in sys.argv
    config = yaml.safe_load((ROOT / "clients" / "smena.yaml").read_text(encoding="utf-8"))
    scenarios = yaml.safe_load((ROOT / "tests" / "scenarios.yaml").read_text(encoding="utf-8"))

    passed = 0
    print(f"\n{BOLD}Прогон {len(scenarios)} диалогов — клиент «{config['client']['name']}»{RESET}\n")

    for scenario in scenarios:
        ok, problems = run_scenario(scenario, config, verbose)
        mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        print(f" {mark}  {scenario['name']}")
        for problem in problems:
            print(f"      {RED}→ {problem}{RESET}")
        if verbose:
            print()
        passed += ok

    failed = len(scenarios) - passed
    colour = GREEN if not failed else RED
    print(f"\n{colour}{passed} из {len(scenarios)} сценариев прошли{RESET}\n")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
