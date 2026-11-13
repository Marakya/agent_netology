"""Агент LangChain `create_agent`: цикл «модель → инструмент → модель» с RAG.

Цикл, вызов инструментов и checkpoint делает LangChain/LangGraph. Приложение
подключается через один middleware и через обёртки инструментов:

* before_model    — проверка отмены/таймаута и сжатие истории (summary + RemoveMessage);
* wrap_model_call — выбор модели (fast / reasoning), системный промпт,
                    доступные инструменты, DLP, бюджет и ретраи вокруг handler();
* after_model     — сохранение финального текста;
* инструменты     — StructuredTool, который делегирует Engine.execute_tool
                    (валидация, подтверждение человеком, идемпотентность, журнал).
"""

from dataclasses import replace

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool
from langgraph.types import Command

from .tools import SPECS


def agent_tools(engine):
    """Все инструменты регистрируются один раз; доступность фильтрует middleware."""

    def build(name):
        def execute(runtime: ToolRuntime, **kwargs):
            # Владелец, run_id и источники берутся из состояния графа, а не из аргументов модели.
            call = {"name": name, "args": kwargs, "id": runtime.tool_call_id}
            return Command(update=engine.execute_tool(runtime.state, call))

        return StructuredTool.from_function(
            execute, name=name, description=SPECS[name][1], args_schema=SPECS[name][0].model_json_schema()
        )

    return [build(name) for name in SPECS]


class ApplicationMiddleware(AgentMiddleware):
    """Единственный middleware агента: связывает цикл LangChain с правилами приложения."""

    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    def before_model(self, state, runtime):
        self.engine.check(state["run_id"])
        summary, removals = self.engine.compact(state)
        # RemoveMessage удаляет свёрнутые сообщения из checkpoint один раз, а не на каждом шаге.
        return {"summary": summary, "messages": removals} if removals else {"summary": summary}

    def wrap_model_call(self, request, handler):
        engine, state = self.engine, request.state
        # DLP до отправки провайдеру: e-mail и секреты не уходят в модель.
        system, *messages = engine.dlp(engine.prompt(state))
        allowed = set(engine.tools.available())
        tools = [t for t in request.tools if t.name in allowed]
        name = engine.model_name(advanced=len(state["messages"]) > 8)

        def invoke(model_name):
            return handler(
                request.override(
                    model=engine.chat_model(model_name),
                    system_message=system,
                    messages=messages,
                    tools=tools,
                    # Один инструмент за шаг: подтверждения и зависимости по источникам однозначны.
                    model_settings={"parallel_tool_calls": False},
                )
            )

        response = engine.call_model(
            state["run_id"], name, invoke, engine.estimate([system, *messages], tools)
        )
        message = response.result[-1]
        if len(message.tool_calls) > 1:
            # Страховка для провайдеров, игнорирующих parallel_tool_calls=False.
            message = message.model_copy(update={"tool_calls": message.tool_calls[:1]})
            response = replace(response, result=[*response.result[:-1], message])
        return response

    def after_model(self, state, runtime):
        message = state["messages"][-1]
        return {"final": "" if message.tool_calls else message.text}


def build_agent(engine, state_schema):
    """Собрать агента. Результат — граф, который Engine встраивает узлом agent."""
    return create_agent(
        # Модель по умолчанию; на каждом шаге wrap_model_call подставляет нужную (fast/reasoning).
        model=engine.chat_model(engine.model_name()),
        tools=agent_tools(engine),
        middleware=[ApplicationMiddleware(engine)],
        state_schema=state_schema,
        name="store_support_agent",
    )
