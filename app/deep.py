"""Адаптер Deep Agents (инструмент deep_analyze).

Многошаговый анализ выполняется целиком в песочнице E2B. Вызовы модели и инструментов
вложенного агента учитываются в бюджете родительской задачи через RootBudgetCallback.
"""

import json
import time

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables.config import ensure_config, merge_configs

from .db import BudgetExceeded, dumps


class RootBudgetCallback(BaseCallbackHandler):
    """Callback LangChain: списывает вызовы вложенного агента из бюджета родительской задачи."""

    raise_error = True

    def __init__(self, settings, db, run_id):
        self.settings, self.db, self.run_id = settings, db, run_id
        self.reservations = {}

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        row = self.db.one("SELECT * FROM runs WHERE id=?", (self.run_id,))
        if row["cancel"]:
            raise InterruptedError("Задача отменена")
        elapsed = row["active_seconds"] + time.time() - row["started_at"]
        if elapsed > self.settings.run_timeout_seconds:
            raise BudgetExceeded("Лимит времени Deep Agents")
        reserve = len(dumps([[m.model_dump() for m in batch] for batch in messages]).encode()) + 4000
        self.db.reserve(
            self.run_id, "model", self.settings.max_model_calls, reserve, self.settings.max_total_tokens
        )
        self.reservations[run_id] = reserve
        self.db.event(self.run_id, "model.started", {"model": "deep-agent", "shared_budget": True})

    def on_llm_end(self, response, *, run_id, **kwargs):
        reserve = self.reservations.pop(run_id, 0)
        usage = (response.llm_output or {}).get("token_usage", {}).get("total_tokens")
        if usage is not None:
            self.db.execute("UPDATE runs SET tokens=tokens+? WHERE id=?", (usage - reserve, self.run_id))

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        name = serialized.get("name", "unknown")
        # Белый список: делегирование (task) запрещено, им управляет только внешний агент.
        if name not in {
            "ls",
            "read_file",
            "write_file",
            "edit_file",
            "glob",
            "grep",
            "execute",
            "write_todos",
        }:
            raise PermissionError("Deep tool is not authorized: " + name)
        self.db.reserve_tool(self.run_id, name, self.settings.max_tool_calls)
        self.db.event(self.run_id, "tool.started", {"name": "deep." + name})


def run_deep_analysis(settings, db, manager, context, task):
    """Запустить Deep Agents в песочнице задачи и экспортировать файлы из /workspace/output."""
    # Импорты внутри функции: зависимости тяжёлые и нужны только при вызове deep_analyze.
    from deepagents import create_deep_agent
    from langchain_e2b import E2BSandbox
    from langchain_openai import ChatOpenAI

    sbx = manager.get(context["run_id"], context["owner"])
    model = ChatOpenAI(
        model=settings.model_reasoning,
        api_key=settings.openai_api_key.get_secret_value(),
        base_url=settings.openai_base_url,
        max_tokens=2000,
        max_retries=0,
        timeout=45,
    )
    backend = E2BSandbox(sandbox=sbx, workdir="/workspace", timeout=60)
    agent = create_deep_agent(
        model=model,
        backend=backend,
        system_prompt="Выполни аналитическую задачу в /workspace. Сеть отключена. Данные в /workspace/input. "
        "Сохрани итоговые артефакты в /workspace/output. Не используй task: делегирование запрещено. "
        "Пиши по-русски. Проверяй вычисления, не выдумывай результат. Не меняй файлы input.",
    )
    callback = RootBudgetCallback(settings, db, context["run_id"])
    result = agent.invoke(
        {"messages": [{"role": "user", "content": task}]},
        # merge_configs сохраняет callbacks внешнего графа, иначе шаги агента выпадут из trace.
        config=merge_configs(ensure_config(), {"callbacks": [callback], "recursion_limit": 24}),
    )
    listing = sbx.run_code(
        "import pathlib,json\nprint(json.dumps([str(p) for p in pathlib.Path('/workspace/output').glob('*') if p.is_file() and not p.is_symlink()][:10]))",
        timeout=10,
    )
    artifacts = []
    if not listing.error and listing.logs.stdout:
        for path in json.loads(listing.logs.stdout[-1]):
            artifacts.append(manager.export(context["run_id"], context["owner"], path))
    return {"findings": result["messages"][-1].text, "artifacts": artifacts, "engine": "deepagents"}
