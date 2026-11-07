"""Allowlisted action metadata, shared by the UI journal and rotating JSONL file."""

import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from .security import redact

LABELS = {
    "run.queued": "Задача поставлена в очередь",
    "run.started": "Агент начал работу",
    "run.recovered": "Возобновление после перезапуска",
    "run.completed": "Задача завершена",
    "run.failed": "Ошибка выполнения",
    "run.limited": "Достигнут лимит задачи",
    "run.cancel_requested": "Запрошена отмена",
    "model.started": "Вызов модели",
    "model.completed": "Ответ модели получен",
    "model.failed": "Ошибка модели",
    "model.retry": "Повторный вызов модели",
    "tool.started": "Запуск инструмента",
    "tool.completed": "Инструмент завершён",
    "tool.failed": "Ошибка инструмента",
    "tool.rejected": "Действие отклонено",
    "approval.required": "Ожидается подтверждение",
    "approval.decided": "Решение пользователя сохранено",
    "task.started": "Вызван специалист",
    "plan.updated": "План обновлён",
    "context.compacted": "Контекст сокращён",
    "sandbox.created": "Песочница создана",
}
TEXT_KEYS = {"mode", "model", "name", "role", "status", "decision", "error_type"}
NUMBER_KEYS = {"attempt", "tokens", "seconds", "removed_messages", "http_status", "source_count"}


def safe_details(payload):
    result = {}
    if not isinstance(payload, dict):
        return result
    for key in TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,100}", value):
            result[key] = redact(value)
    for key in NUMBER_KEYS:
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = value
    if isinstance(payload.get("ok"), bool):
        result["ok"] = payload["ok"]
    for key in ("tasks", "approvals", "citations", "artifacts"):
        if isinstance(payload.get(key), list):
            result[key + "_count"] = len(payload[key])
    return result


def entry(run_id, event_type, payload, created, event_id=None):
    details = safe_details(payload)
    level = "ERROR" if event_type.endswith("failed") else "INFO"
    if details.get("status") == "failed" or details.get("ok") is False:
        level = "ERROR"
    elif event_type in {"run.limited", "model.retry"}:
        level = "WARNING"
    return {
        "id": event_id,
        "time": datetime.fromtimestamp(created, timezone.utc).isoformat(),
        "run_id": run_id,
        "event": event_type,
        "action": LABELS.get(event_type, "Событие агента"),
        "level": level,
        "details": details,
    }


class ActionLog:
    def __init__(self, directory):
        self.handler = None
        self.logger = logging.Logger("agent.actions", level=logging.INFO)
        self.logger.propagate = False
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self.handler = RotatingFileHandler(
                directory / "agent.jsonl", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            self.handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(self.handler)
        except OSError:
            logging.getLogger(__name__).warning("Action file unavailable; journal remains in SQLite")

    def write(self, run_id, event_type, payload, created, event_id=None):
        if self.handler:
            data = entry(run_id, event_type, payload, created, event_id)
            self.logger.log(getattr(logging, data["level"]), json.dumps(data, ensure_ascii=False))

    def close(self):
        if self.handler:
            self.logger.removeHandler(self.handler)
            self.handler.close()
