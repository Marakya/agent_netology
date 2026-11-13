"""Движок задач: граф LangGraph `agent → finish` поверх агента LangChain.

* agent  — create_agent (см. langchain_agent.py): LLM + инструменты + RAG;
* finish — проверка источников, reviewer-проход LLM, сборка ответа Answer.

Что находится в этом модуле (сверху вниз):

1. State и вспомогательные функции (is_transient, cited).
2. Engine — жизненный цикл задачи: submit → work → complete, восстановление после сбоя.
3. Engine — единая граница вызова модели call_model: бюджет, DLP, журнал, ретрай.
4. Engine — контекст: сжатие истории (compact) и сборка промпта (prompt).
5. Engine — единая точка выполнения инструментов execute_tool: идемпотентность,
   валидация, подтверждение человеком, бюджет, журнал.
6. Engine — специалисты (инструмент task) и финальный узел графа finish_node.
"""

import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

from langchain.agents import AgentState
from langchain.agents.middleware import ModelResponse
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from langgraph.types import Command, interrupt

from .db import BudgetExceeded, digest, dumps
from .langchain_agent import build_agent
from .models import Answer
from .prompts import REVIEWER, SUMMARIZER, SYSTEM, specialist_system
from .security import redact
from .support import format_case, update_ticket
from .tools import APPROVAL_TOOLS, schemas
from .tracing import Tracing

log = logging.getLogger(__name__)


WORKERS = 4  # сколько задач выполняется одновременно (потоки пула)
CONTEXT_LIMIT = 20000  # порог сжатия истории, символы JSON
KEEP_FULL_TOOL_RESULTS = 6  # сколько последних сообщений не урезать в промпте
TOOL_RESULT_PREVIEW = 1500  # до скольких символов урезается старый результат инструмента
MAX_TOOL_MESSAGE = 30000  # результат длиннее уходит в артефакт, модель получает выдержку
SPECIALIST_STEPS = 3  # максимум шагов «модель → инструменты» у специалиста
MODEL_TIMEOUT_SECONDS = 45
MODEL_MAX_OUTPUT_TOKENS = 2000
# Инструменты песочницы: после сбоя неизвестно, успел ли код выполниться, поэтому не повторяем.
NON_REPLAYABLE_TOOLS = {"run_python", "execute", "deep_analyze"}


class State(AgentState, total=False):
    """Состояние графа. Сохраняется в checkpoint после каждого шага."""

    messages: Annotated[list[BaseMessage], add_messages]
    run_id: str
    owner: str
    sources: list[dict]  # все источники (RAG/web), полученные инструментами в этом run
    artifacts: list[dict]
    limitations: list[str]
    final: str  # последний текст модели без tool_calls
    summary: str  # сжатая история старых ходов
    answer: dict | None  # итог finish
    support_case: dict | None  # карточка из publish_support_draft


def is_transient(exc, status=None):
    """Временный сбой провайдера, который имеет смысл повторить один раз.

    openai.APITimeoutError/APIConnectionError и httpx-исключения не наследуют
    встроенные TimeoutError/ConnectionError, поэтому проверяем их явно.
    """
    import httpx
    import openai

    if status in {408, 409, 429, 500, 502, 503, 504}:
        return True
    return isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            openai.APIConnectionError,
            httpx.TimeoutException,
            httpx.NetworkError,
        ),
    )


def cited(sources, refs):
    """Оставить источники, на которые ссылается ответ.

    refs — список source_id (карточка поддержки) или текст ответа с метками [source_id].
    Если текст не содержит ни одной метки, возвращаются все найденные источники.
    """
    if isinstance(refs, str):
        used = [s for s in sources if f"[{s['source_id']}]" in refs]
        return used or sources
    return [s for s in sources if s["source_id"] in refs]


def last_message(result):
    """AIMessage из ответа модели: ChatModel.invoke или handler() в wrap_model_call."""
    return result.result[-1] if isinstance(result, ModelResponse) else result


def tool_error(exc):
    """Текст ошибки инструмента для модели.

    ValueError — «ожидаемая» ошибка (валидация, политика): её текст нужен модели, чтобы
    исправить аргументы. Для остальных исключений отдаём только тип: в тексте могут быть
    внутренние детали.
    """
    if isinstance(exc, ValueError):
        return {"error": redact(str(exc))[:500]}
    return {"error": "Ошибка инструмента: " + type(exc).__name__}


class Engine:
    def __init__(self, settings, db, toolbox):
        self.settings, self.db, self.tools = settings, db, toolbox
        self.tools.delegate = self.specialist  # инструмент task вызывает специалиста
        self.tracing = Tracing(settings)
        self.pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="agent")
        # active — задачи, которые сейчас в пуле; защищает от двойного запуска одной задачи.
        self.active, self.lock = set(), threading.RLock()
        self.connection = sqlite3.connect(
            str(settings.data_dir / "checkpoints.sqlite"), check_same_thread=False
        )
        self.saver = SqliteSaver(self.connection)
        # Внешний граф: START → agent (вложенный граф create_agent) → finish → END.
        builder = StateGraph(State)
        builder.add_node("agent", build_agent(self, State))
        builder.add_node("finish", self.finish_node)
        builder.add_edge(START, "agent")
        builder.add_edge("agent", "finish")
        builder.add_edge("finish", END)
        self.graph = builder.compile(checkpointer=self.saver)

    # ---- Жизненный цикл задачи ------------------------------------------------

    def recover(self):
        """При старте приложения вернуть в очередь задачи, прерванные остановкой процесса."""
        self.db.execute(
            "UPDATE runs SET active_seconds=active_seconds+MAX(0,?-started_at) WHERE status='running' AND started_at>0",
            (time.time(),),
        )
        self.db.execute("UPDATE runs SET status='queued' WHERE status='running'")
        for run in self.db.all("SELECT id FROM runs WHERE status='queued'"):
            self.db.event(run["id"], "run.recovered", {})
            self.submit(run["id"])

    def config(self, thread_id):
        """Конфигурация графа: checkpoint ведётся по беседе (thread_id)."""
        return {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}

    def submit(self, run_id):
        """Поставить задачу в пул потоков (повторный submit той же задачи игнорируется)."""
        with self.lock:
            if run_id in self.active:
                return
            self.active.add(run_id)
        self.pool.submit(self.work, run_id)

    def check(self, run_id):
        """Контрольная точка: прервать задачу, если она отменена или вышла за лимит времени."""
        row = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not row or row["cancel"]:
            raise InterruptedError("Задача отменена")
        elapsed = row["active_seconds"] + (time.time() - row["started_at"] if row["started_at"] else 0)
        if elapsed > self.settings.run_timeout_seconds:
            raise BudgetExceeded("Истёк лимит времени задачи")
        return row

    def work(self, run_id):
        """Выполнить задачу в потоке пула: запустить (или продолжить) граф и сохранить итог."""
        try:
            row = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
            if not row or row["status"] not in {"queued", "running"}:
                return
            self.db.execute("UPDATE runs SET status='running',started_at=? WHERE id=?", (time.time(), run_id))
            self.db.event(run_id, "run.started", {"model": self.settings.model_fast})
            config = self.config(row["thread_id"])
            snapshot = self.graph.get_state(config)

            # Определяем, с чего начать: возможны четыре ситуации.
            resuming = row["resume"] and any(task.interrupts for task in snapshot.tasks)
            same_run = snapshot.values.get("run_id") == run_id
            if resuming:
                # 1. Человек принял решение по подтверждению — продолжаем с interrupt().
                inputs = Command(resume=json.loads(row["resume"]))
            elif same_run and snapshot.next:
                # 2. Процесс упал посреди этой задачи — продолжаем с последнего checkpoint.
                inputs = None
            elif same_run:
                # 3. Граф уже дошёл до конца, но приложение не успело сохранить результат:
                #    берём ответ из checkpoint, модель повторно не вызываем.
                self.complete(run_id, self.answer_from_checkpoint(snapshot))
                return
            else:
                # 4. Новый ход беседы.
                inputs = self.new_turn(run_id, row, snapshot)

            traced = config | self.tracing.run_config(run_id, row["thread_id"], row["owner"])
            result = self.graph.invoke(inputs, config=traced)
            if result.get("__interrupt__"):
                self.wait_for_approval(run_id)
            else:
                self.complete(run_id, Answer.model_validate(result["answer"]))
        except (BudgetExceeded, InterruptedError) as exc:
            # Отмена и исчерпание бюджета — штатные исходы, а не ошибки.
            status = "cancelled" if isinstance(exc, InterruptedError) else "partial"
            self.db.event(
                run_id,
                "run.limited" if status == "partial" else "run.cancel_requested",
                {"error_type": type(exc).__name__},
            )
            self.complete(run_id, Answer(answer=str(exc), status=status, limitations=[str(exc)]))
        except Exception as exc:
            # В журнал и пользователю — только тип ошибки: в тексте может быть содержимое запроса.
            log.error("run %s failed: %s", run_id, type(exc).__name__)
            self.db.event(run_id, "run.failed", {"error_type": type(exc).__name__})
            reason = (
                "Ошибка выполнения: "
                + type(exc).__name__
                + ". Проверьте конфигурацию и доступность провайдеров."
            )
            self.complete(run_id, Answer(answer=reason, status="failed", limitations=[reason]))
        finally:
            # Учитываем отработанное время (таймаут считается по активному времени, без ожидания человека).
            self.db.execute(
                "UPDATE runs SET active_seconds=active_seconds+MAX(0,?-started_at),started_at=0 WHERE id=? AND started_at>0",
                (time.time(), run_id),
            )
            self.tools.sandbox.close_run(run_id)
            with self.lock:
                self.active.discard(run_id)
            # Решение по подтверждению могло прийти, пока поток завершался: запускаем задачу снова.
            pending = self.db.one("SELECT status FROM runs WHERE id=?", (run_id,))
            if pending and pending["status"] == "queued":
                self.submit(run_id)

    @staticmethod
    def answer_from_checkpoint(snapshot):
        """Ответ завершённого графа из checkpoint (результат приложения не успел сохраниться)."""
        saved = snapshot.values.get("answer")
        if saved:
            return Answer.model_validate(saved)
        return Answer(
            answer=snapshot.values.get("final") or "Задача завершена без сохранённого ответа.",
            status="partial",
            limitations=["Восстановление checkpoint предыдущей версии"],
        )

    def new_turn(self, run_id, row, snapshot):
        """Входные данные графа для нового хода беседы: сообщение и обнулённые поля задачи."""
        inputs = {
            "messages": [HumanMessage(content=row["message"], id=run_id)],
            "run_id": run_id,
            "owner": row["owner"],
            "sources": [],
            "artifacts": [],
            "limitations": [],
            "final": "",
            "answer": None,
            "support_case": None,
        }
        if snapshot.next:
            # Предыдущая задача оборвалась посреди цикла (отмена, сбой). В checkpoint мог остаться
            # вызов инструмента без ответа — модель не должна его увидеть. Заменяем историю графа
            # на видимую историю беседы из SQLite.
            history = self.db.all(
                "SELECT id,role,content FROM messages WHERE thread_id=? ORDER BY created,id",
                (row["thread_id"],),
            )
            inputs["messages"] = [RemoveMessage(id=REMOVE_ALL_MESSAGES)] + [
                (HumanMessage if m["role"] == "user" else AIMessage)(content=m["content"], id=m["id"])
                for m in history
            ]
            inputs["summary"] = ""
        return inputs

    def wait_for_approval(self, run_id):
        """Граф остановился на interrupt(): перевести задачу в ожидание решения человека."""
        with self.db.transaction() as conn:
            cancelled = conn.execute("SELECT cancel FROM runs WHERE id=?", (run_id,)).fetchone()
            if cancelled and cancelled[0]:
                raise InterruptedError("Задача отменена")
            conn.execute("UPDATE runs SET status='waiting_approval',resume=NULL WHERE id=?", (run_id,))
        self.db.event(
            run_id,
            "approval.required",
            {
                "approvals": self.db.all(
                    "SELECT id,tool,args,hash FROM approvals WHERE run_id=? AND status='pending'",
                    (run_id,),
                )
            },
        )

    def complete(self, run_id, answer):
        """Сохранить итог задачи.

        Статус, сообщение ассистента, закрытие подтверждений и событие run.completed пишутся
        одной транзакцией. Повторный вызов ничего не меняет (result уже записан), поэтому
        восстановление после сбоя безопасно.
        """
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["result"] is not None:
                return
            if row["cancel"]:
                answer.status = "cancelled"
                if "Пользователь отменил задачу" not in answer.limitations:
                    answer.limitations.append("Пользователь отменил задачу")
            conn.execute(
                "UPDATE runs SET status=?,result=?,resume=NULL WHERE id=?",
                (answer.status, answer.model_dump_json(), run_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?)",
                (run_id + "_answer", row["thread_id"], "assistant", answer.answer, time.time()),
            )
            conn.execute(
                "UPDATE approvals SET status='closed' WHERE run_id=? AND status='pending'", (run_id,)
            )
            completed_at = time.time()
            completed_event = conn.execute(
                "INSERT INTO events(run_id,type,payload,created) VALUES(?,?,?,?)",
                (run_id, "run.completed", answer.model_dump_json(), completed_at),
            ).lastrowid
        self.db.action_log.write(run_id, "run.completed", answer.model_dump(), completed_at, completed_event)

    # ---- Модель ---------------------------------------------------------------

    def model_name(self, advanced=False):
        """Выбор модели: при MODEL_STRATEGY=dynamic сложные шаги идут на MODEL_REASONING."""
        if advanced and self.settings.model_strategy == "dynamic":
            return self.settings.model_reasoning
        return self.settings.model_fast

    def chat_model(self, name):
        # Импорт внутри функции: тесты подменяют langchain_openai.ChatOpenAI заглушкой.
        from langchain_openai import ChatOpenAI

        # Ретраи делает call_model (с учётом бюджета), поэтому у клиента max_retries=0.
        return ChatOpenAI(
            model=name,
            api_key=self.settings.openai_api_key.get_secret_value(),
            base_url=self.settings.openai_base_url,
            timeout=MODEL_TIMEOUT_SECONDS,
            max_retries=0,
            max_tokens=MODEL_MAX_OUTPUT_TOKENS,
        )

    @staticmethod
    def dlp(messages):
        """Маскировать e-mail и секреты перед отправкой провайдеру."""
        return [
            m.model_copy(update={"content": redact(m.content)}) if isinstance(m.content, str) else m
            for m in messages
        ]

    @staticmethod
    def estimate(messages, tools=()):
        """Резерв токенов: байты UTF-8 — заведомо верхняя оценка BPE-токенов; остаток возвращается."""
        payload = [t if isinstance(t, dict) else convert_to_openai_tool(t) for t in tools]
        return (
            len(dumps([m.model_dump() for m in messages]).encode())
            + len(dumps(payload).encode())
            + MODEL_MAX_OUTPUT_TOKENS
        )

    def call_model(self, run_id, name, invoke, reserved):
        """Единая граница вызова LLM: бюджет, журнал, один ретрай временного сбоя.

        invoke(model_name) выполняет сам вызов и возвращает AIMessage или ModelResponse.
        reserved — оценка токенов запроса, которая резервируется в бюджете до вызова.
        """
        self.check(run_id)
        for attempt in range(2):
            self.db.reserve(
                run_id, "model", self.settings.max_model_calls, reserved, self.settings.max_total_tokens
            )
            self.db.event(run_id, "model.started", {"model": name, "attempt": attempt + 1})
            started = time.monotonic()
            try:
                result = invoke(name)
            except Exception as exc:
                # Неудачный вызов не потребил токены: возвращаем резерв, иначе бюджет «утекает».
                self.db.execute("UPDATE runs SET tokens=tokens-? WHERE id=?", (reserved, run_id))
                status = getattr(exc, "status_code", None)
                # Тело ошибки провайдера не логируем: в нём может быть текст запроса.
                self.db.event(
                    run_id,
                    "model.failed",
                    {
                        "model": name,
                        "error_type": type(exc).__name__,
                        "http_status": status,
                        "attempt": attempt + 1,
                    },
                )
                if attempt or not is_transient(exc, status):
                    raise
                name = self.settings.model_fallback or name
                self.db.event(run_id, "model.retry", {"model": name, "attempt": attempt + 2})
                time.sleep(0.5)
                continue
            # Успех: заменяем резерв фактическим расходом токенов.
            usage = last_message(result).usage_metadata or {}
            actual = usage.get("total_tokens", reserved)
            self.db.execute("UPDATE runs SET tokens=tokens+? WHERE id=?", (actual - reserved, run_id))
            self.db.event(
                run_id,
                "model.completed",
                {"model": name, "tokens": actual, "seconds": round(time.monotonic() - started, 3)},
            )
            return result
        raise RuntimeError("Model unavailable")

    def chat(self, messages, run_id, tools=None, advanced=False):
        """Вызов LLM вне агентного цикла: summary, reviewer, специалисты."""
        messages = self.dlp(messages)

        def invoke(name):
            model = self.chat_model(name)
            return (model.bind_tools(tools, parallel_tool_calls=False) if tools else model).invoke(messages)

        return self.call_model(
            run_id, self.model_name(advanced), invoke, self.estimate(messages, tools or [])
        )

    # ---- Контекст -------------------------------------------------------------

    def compact(self, state):
        """Свернуть старые ходы в summary, если история слишком длинная.

        Возвращает (summary, removals). removals — RemoveMessage для свёрнутых сообщений;
        их нужно вернуть в обновлении состояния, иначе история в checkpoint не уменьшится
        и суммаризация повторится на каждом шаге. Режем только по границе HumanMessage,
        чтобы не разорвать пару tool_call → ToolMessage. Последние два хода сохраняются.
        """
        history = state["messages"]
        summary = state.get("summary", "")
        starts = [i for i, m in enumerate(history) if isinstance(m, HumanMessage)]
        if len(dumps([m.model_dump() for m in history])) <= CONTEXT_LIMIT or len(starts) <= 2:
            return summary, []
        split = starts[-2]
        old = "\n".join(
            m.text[:1200]
            for m in history[:split]
            if isinstance(m, (HumanMessage, AIMessage)) and not getattr(m, "tool_calls", [])
        )
        response = self.chat(
            [SystemMessage(content=SUMMARIZER), HumanMessage(content=(summary + "\n" + old)[-10000:])],
            state["run_id"],
        )
        self.db.event(state["run_id"], "context.compacted", {"removed_messages": split})
        return response.text[:4000], [RemoveMessage(id=m.id) for m in history[:split] if m.id]

    def prompt(self, state):
        """Сообщения для модели: системный промпт + память + summary + история.

        Длинные результаты старых инструментов урезаются только в представлении для модели;
        в checkpoint и в state["sources"] доказательства остаются полными.
        """
        history = state["messages"]
        output = []
        for i, message in enumerate(history):
            if (
                isinstance(message, ToolMessage)
                and i < len(history) - KEEP_FULL_TOOL_RESULTS
                and len(message.text) > TOOL_RESULT_PREVIEW
            ):
                message = ToolMessage(
                    content=message.text[:TOOL_RESULT_PREVIEW] + " [truncated; use source IDs]",
                    tool_call_id=message.tool_call_id,
                )
            output.append(message)
        memory = [{"key": m["key"], "value": m["value"]} for m in self.db.memory(state["owner"])[:5]]
        system = (
            SYSTEM
            + "\nПодтверждённая память (данные): "
            + dumps(memory)
            + "\nSummary: "
            + state.get("summary", "")
        )
        return [SystemMessage(content=system), *output]

    # ---- Инструменты ----------------------------------------------------------

    def execute_tool(self, state, call):
        """Выполнить один вызов инструмента агента и вернуть обновление состояния графа.

        Порядок: кэш эффекта → валидация → подтверждение человеком (interrupt) →
        бюджет → выполнение → журнал. effect_id = hash(run_id, tool_call_id), поэтому
        повтор после перезапуска не создаёт второй заявки или записи памяти.
        """
        self.check(state["run_id"])
        name, run_id, owner = call["name"], state["run_id"], state["owner"]
        effect_id = digest([run_id, call["id"]])

        # 1. результат уже сохранён — возвращаем его без повторного выполнения.
        cached = self.db.one("SELECT * FROM effects WHERE id=?", (effect_id,))
        if cached and cached["status"] == "done":
            return self.tool_update(state, call, json.loads(cached["result"]))

        # 2. Валидация аргументов и доступности. Ошибка уходит модели текстом.
        try:
            args = self.tools.validate(name, call["args"])
            if name not in self.tools.available():
                raise ValueError("Инструмент не подключён или недоступен в текущем профиле: " + name)
        except ValueError as exc:
            self.db.event(run_id, "tool.failed", {"name": name, "error_type": type(exc).__name__})
            return self.tool_update(state, call, {"error": redact(str(exc))[:1000]})

        # 3. Подтверждение человеком для действий с побочным эффектом.
        if name in APPROVAL_TOOLS:
            args = self.confirm(run_id, owner, name, args, effect_id)
            if args is None:
                self.db.event(run_id, "tool.rejected", {"name": name})
                return self.tool_update(
                    state, call, {"rejected": True, "message": "Пользователь отклонил действие"}
                )
        if cached and cached["status"] == "running" and name in NON_REPLAYABLE_TOOLS:
            return self.tool_update(
                state,
                call,
                {
                    "error": "Предыдущий результат sandbox неизвестен после сбоя. Повторите явно в новой задаче."
                },
            )

        # 4. Бюджет: общий лимит вызовов и лимит на конкретный инструмент.
        self.db.reserve_tool(run_id, name, self.settings.max_tool_calls)

        # 5. Выполнение. Запись effects со статусом running помечает «начато, итог неизвестен».
        self.db.execute(
            "INSERT OR IGNORE INTO effects VALUES(?,?,?,?,?)", (effect_id, run_id, name, "running", None)
        )
        self.db.event(run_id, "tool.started", {"name": name})
        started = time.monotonic()
        try:
            result = self.tools.execute(
                name,
                args,
                {
                    "run_id": run_id,
                    "owner": owner,
                    "effect_id": effect_id,
                    "sources": state.get("sources", []),
                },
            )
        except (BudgetExceeded, InterruptedError):
            raise  # отмена и бюджет останавливают всю задачу, а не один инструмент
        except Exception as exc:
            self.db.event(run_id, "tool.failed", {"name": name, "error_type": type(exc).__name__})
            result = tool_error(exc)

        # 6. Сохраняем результат (для идемпотентности) и пишем журнал.
        self.db.execute("UPDATE effects SET status='done',result=? WHERE id=?", (dumps(result), effect_id))
        self.db.event(
            run_id,
            "tool.completed",
            {
                "name": name,
                "seconds": round(time.monotonic() - started, 2),
                # run_python возвращает {"error": None} при успехе — важно значение, а не наличие ключа.
                "ok": not result.get("error"),
                "source_count": len(result.get("sources", [])),
            },
        )
        return self.tool_update(state, call, result)

    def confirm(self, run_id, owner, name, args, effect_id):
        """Получить решение человека по действию с побочным эффектом.

        Первый вход: сохраняет предложение в approvals и останавливает граф через interrupt().
        После решения граф заново входит в узел инструмента, и функция доходит до конца.
        Возвращает аргументы для выполнения (исходные или отредактированные человеком)
        либо None, если действие отклонено.
        """
        if name == "save_memory" and args.get("expected_version") is None:
            # Фиксируем версию памяти на момент предложения: если запись изменится, пока человек
            # думает, сохранение завершится конфликтом, а не затрёт более новое значение.
            current = self.db.one(
                "SELECT version FROM memories WHERE owner=? AND key=?", (owner, args["key"])
            )
            args["expected_version"] = current["version"] if current else 0
        self.db.execute(
            "INSERT OR IGNORE INTO approvals VALUES(?,?,?,?,?,?,?,?,?)",
            (effect_id, run_id, owner, name, dumps(args), digest([name, args]), "pending", None, time.time()),
        )
        approval = self.db.one("SELECT * FROM approvals WHERE id=?", (effect_id,))
        # После перезапуска используем сохранённое предложение, а не аргументы, вычисленные заново:
        # человек подтверждает ровно то, что видел (action_hash).
        args = json.loads(approval["args"])
        if approval["status"] == "pending":
            interrupt({"approval_id": effect_id, "tool": name, "args": args, "action_hash": approval["hash"]})
            approval = self.db.one("SELECT * FROM approvals WHERE id=?", (effect_id,))
        decision = json.loads(approval["decision"] or "{}")
        if decision.get("decision") == "reject":
            return None
        if decision.get("decision") not in {"approve", "edit"}:
            raise ValueError("Нет действующего подтверждения")
        if decision.get("decision") == "edit":
            args = self.tools.validate(name, decision["edited_args"])
        return args

    def tool_update(self, state, call, result):
        """Превратить результат инструмента в обновление состояния графа (ToolMessage + поля задачи)."""
        limits = state.get("limitations", []) + ([result["error"]] if result.get("error") else [])
        content = dumps(result)
        if len(content) > MAX_TOOL_MESSAGE:
            # Слишком большой результат: целиком в артефакт, модели — выдержка.
            artifact = self.tools.artifacts.save(
                state["owner"], state["run_id"], "tool-result.json", content.encode()
            )
            content = dumps({"excerpt": content[:12000], "truncated": True, "artifact": artifact})
            result.setdefault("artifacts", []).append(artifact)
        return {
            "messages": [ToolMessage(content=content, tool_call_id=call["id"])],
            "sources": state.get("sources", []) + result.get("sources", []),
            "artifacts": state.get("artifacts", []) + result.get("artifacts", []),
            "limitations": limits,
            "support_case": result.get("support_case", state.get("support_case")),
        }

    def specialist(self, role, task, context):
        """Инструмент task: выполнить подзадачу специалистом в отдельном контексте.

        Специалист не видит историю беседы, ограничен SPECIALIST_STEPS шагами и набором
        инструментов своей роли. Инструменты с подтверждением ему недоступны (вложенного
        interrupt нет), делегировать дальше он не может. Вызовы идут в бюджет родительской задачи.
        """
        run_id = context["run_id"]
        self.db.event(run_id, "task.started", {"role": role, "task": task[:300]})
        messages = [SystemMessage(content=specialist_system(role)), HumanMessage(content=task)]
        names = set(self.tools.available(role)) - APPROVAL_TOOLS
        # Вложенных подтверждений нет, поэтому действия в песочнице аналитику недоступны.
        names -= {"files", "edit_file", "export_file"} if role == "analyst" else set()
        sources, artifacts = [], []
        for _ in range(SPECIALIST_STEPS):
            response = self.chat(messages, run_id, schemas(sorted(names)), advanced=role != "researcher")
            messages.append(response)
            if not response.tool_calls:
                return {"findings": response.text, "sources": sources, "artifacts": artifacts}
            for call in response.tool_calls:
                if call["name"] not in names:
                    result = {"error": "Нет доступа у этой роли"}
                else:
                    self.db.reserve_tool(run_id, call["name"], self.settings.max_tool_calls)
                    try:
                        result = self.tools.execute(
                            call["name"], self.tools.validate(call["name"], call["args"]), context
                        )
                    except (BudgetExceeded, InterruptedError):
                        raise
                    except Exception as exc:
                        result = tool_error(exc)
                sources.extend(result.get("sources", []))
                artifacts.extend(result.get("artifacts", []))
                messages.append(ToolMessage(content=dumps(result), tool_call_id=call["id"]))
        return {
            "findings": "Лимит шагов подагента; собраны источники",
            "sources": sources,
            "artifacts": artifacts,
        }

    # ---- Финал ----------------------------------------------------------------

    def finish_node(self, state):
        """Узел finish: проверить источники, прогнать reviewer и собрать итоговый Answer."""
        sources = list({s["source_id"]: s for s in state.get("sources", [])}.values())
        # Повторная проверка доступа: документ мог быть удалён во время ожидания подтверждения.
        sources = [
            s
            for s in sources
            if s["kind"] != "document"
            or self.db.one(
                "SELECT id FROM documents WHERE id=? AND owner=? AND state='ready'",
                (s["document_id"], state["owner"]),
            )
        ]
        answer = Answer(
            answer=state.get("final") or "Задача завершена без текстового ответа.",
            citations=sources,
            artifacts=state.get("artifacts", []),
            limitations=state.get("limitations", []),
            status="partial" if state.get("limitations") else "completed",
            support_case=state.get("support_case"),
        )
        # Pydantic копирует dict при валидации: правим карточку внутри answer, а не исходный dict.
        case = answer.support_case
        if sources:
            # Reviewer: второй проход LLM убирает утверждения, не подтверждённые источниками.
            self.db.event(
                state["run_id"],
                "task.started",
                {"role": "reviewer", "task": "Проверить выводы по источникам"},
            )
            try:
                draft = case["draft"] if case else answer.answer
                review = self.chat(
                    [
                        SystemMessage(content=REVIEWER),
                        HumanMessage(content=dumps({"answer": draft, "evidence": sources})),
                    ],
                    state["run_id"],
                )
                # Пустой ответ reviewer не должен стирать исходный ответ.
                answer.answer = review.text.strip() or answer.answer
                if case:
                    case["draft"] = answer.answer[:2000]
            except BudgetExceeded:
                answer.status = "partial"
                answer.limitations.append("Бюджета на финальный review не хватило")
        if case:
            # Черновик мог измениться после review: пересобираем предложение заявки и текст ответа.
            update_ticket(case)
            answer.answer = format_case(case)
        answer.citations = cited(answer.citations, case["source_ids"] if case else answer.answer)
        return {"answer": answer.model_dump()}

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.tracing.close()
        self.tools.sandbox.close()
        self.connection.close()
