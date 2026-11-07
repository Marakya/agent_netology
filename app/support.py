"""Карточка ответа поддержки, которую публикует агент инструментом publish_support_draft."""

from pathlib import Path

# Учебный магазин «Лист»: четыре регламента для загрузки в базу знаний (RAG).
SAMPLE_DIR = Path(__file__).parent / "sample_store"


def bullets(items):
    return "".join(f"\n- {item}" for item in items)


def support_case(args):
    """Собрать карточку из проверенных аргументов инструмента (черновик пишет LLM)."""
    case = {key: value for key, value in args.items() if key != "source_ids"}
    case["source_ids"] = list(dict.fromkeys(args["source_ids"]))
    update_ticket(case)
    return case


def update_ticket(case):
    """Предложение заявки для create_ticket; создаётся только после подтверждения человеком."""
    case["ticket"] = {
        "title": case["category"],
        "body": f"Ответственный: {case['assignee']}\nЧерновик ответа:\n{case['draft']}\n"
        f"Уточнения:{bullets(case['questions'])}\nОснование: {case['reason']}\n"
        f"Источники: {', '.join(case['source_ids'])}",
    }


def format_case(case):
    refs = " ".join(f"[{sid}]" for sid in case["source_ids"])
    return (
        f"{case['category']}\n\nЧерновик ответа покупателю:\n{case['draft']}\n\n"
        f"Уточнения:{bullets(case['questions']) or ' не требуются'}\n"
        f"Для менеджера: {case['reason']}\nОтветственный: {case['assignee']}\n"
        f"Источники: {refs or 'Нет подтверждённых источников — нужна ручная проверка'}\n"
        "Ответ не отправлен. Заявка создаётся отдельно после подтверждения."
    )
