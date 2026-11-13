"""Langfuse: настоящий SDK и настоящие callbacks, вместо отправки в сеть — экспорт в память."""

import json
import time
import uuid
from functools import partial
from types import SimpleNamespace

import pytest
from conftest import ScriptedChat
from fastapi.testclient import TestClient
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.config import Settings
from app.deep import RootBudgetCallback, run_deep_analysis
from app.main import create_app
from app.tracing import Tracing, clean

EMAIL = "buyer@example.com"
PASSWORD = "password: hunter2"
PROVIDER_ERROR = "provider echoed request text"


class TracedChat(BaseChatModel):
    """Сценарий из conftest, но как настоящий Runnable: только такой вызов виден callbacks."""

    has_tools: bool = False
    fail: bool = False

    def __init__(self, **kwargs):
        super().__init__(fail=kwargs.get("fail", False), has_tools=kwargs.get("has_tools", False))

    @property
    def _llm_type(self):
        return "traced-test"

    def bind_tools(self, tools, **kwargs):
        return TracedChat(has_tools=bool(tools), fail=self.fail)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.fail:
            raise ValueError(PROVIDER_ERROR)
        script = ScriptedChat()
        script.tools = self.has_tools
        return ChatResult(generations=[ChatGeneration(message=script.invoke(messages))])


@pytest.fixture
def traced(tmp_path, monkeypatch):
    exporter = InMemorySpanExporter()

    class ExportedLangfuse(Langfuse):
        def __init__(self, **kwargs):
            super().__init__(span_exporter=exporter, **kwargs)

    monkeypatch.setattr("langchain_openai.ChatOpenAI", TracedChat)
    monkeypatch.setattr("langfuse.Langfuse", ExportedLangfuse)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        openai_api_key="test-openai-key",
        # SDK хранит клиента по public key: у каждого теста свой, чтобы не делить экспорт.
        langfuse_public_key="pk-lf-" + uuid.uuid4().hex,
        langfuse_secret_key="sk-lf-test",
        langfuse_base_url="http://127.0.0.1:9",
    )
    with TestClient(create_app(settings)) as client:
        yield client, exporter


def start(client, message, thread_id=None):
    thread_id = thread_id or client.post("/api/threads", json={"title": "t"}).json()["id"]
    run_id = client.post(
        f"/api/threads/{thread_id}/runs",
        json={"message": message},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    ).json()["id"]
    return thread_id, run_id


def wait(client, run_id, statuses=("completed", "partial", "failed", "waiting_approval")):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in statuses:
            return run
        time.sleep(0.05)
    pytest.fail("Run did not settle")


def spans(client, exporter):
    client.app.state.services.engine.tracing.client.flush()
    return exporter.get_finished_spans()


def everything(items):
    """Всё, что уйдёт наружу: атрибуты, статусы и события каждого span."""
    return "\n".join(json.dumps(json.loads(item.to_json()), ensure_ascii=False) for item in items)


def test_tracing_is_off_without_both_keys():
    assert Tracing(Settings(_env_file=None)).run_config("r", "t", "o") == {}
    partial_keys = Settings(_env_file=None, langfuse_public_key="pk-lf-only")
    assert Tracing(partial_keys).run_config("r", "t", "o") == {}


def test_clean_masks_nested_and_unknown_values():
    class Unknown:
        def __str__(self):
            return "owner " + EMAIL

    masked = clean({"a": [EMAIL, (PASSWORD,)], "b": Unknown(), "c": 1, "d": None})
    assert EMAIL not in json.dumps(masked) and "hunter2" not in json.dumps(masked)
    assert masked["c"] == 1 and masked["d"] is None


def test_run_trace_has_models_tools_identity_and_masked_content(traced):
    client, exporter = traced
    client.post("/api/support/sample")
    message = f"Обращение: наушники сломались, {EMAIL}, {PASSWORD}"
    thread_id, run_id = start(client, message)
    assert wait(client, run_id)["status"] in {"completed", "partial"}
    items = spans(client, exporter)
    attributes = [item.attributes for item in items]
    generations = [a for a in attributes if a.get("langfuse.observation.type") == "generation"]
    # Агент (поиск и публикация), финальный ответ и reviewer вне цикла агента.
    assert len(generations) >= 3
    assert any(a.get("langfuse.observation.type") == "tool" for a in attributes)
    assert len({item.context.trace_id for item in items}) == 1
    assert any(a.get("session.id") == thread_id for a in attributes)
    assert any(a.get("user.id") for a in attributes)
    assert any(a.get("langfuse.trace.name") == "support-run" for a in attributes)
    dump = everything(items)
    assert "наушники" in dump  # контент виден, иначе проверка маскирования ничего не значит
    assert EMAIL not in dump and "hunter2" not in dump


def test_approval_resume_stays_in_one_trace(traced):
    client, exporter = traced
    _, run_id = start(client, "/ticket Проверка\nТекст заявки")
    run = wait(client, run_id, ("waiting_approval",))
    before = {item.context.trace_id for item in spans(client, exporter)}
    approval = run["approvals"][0]
    client.post(
        "/api/approvals/" + approval["id"], json={"decision": "approve", "action_hash": approval["hash"]}
    )
    assert wait(client, run_id, ("completed", "partial"))["status"]
    items = spans(client, exporter)
    assert {item.context.trace_id for item in items} == before and len(before) == 1
    assert client.app.state.services.db.all("SELECT * FROM tickets")
    # Ожидание подтверждения — обычный ход работы графа, а не ошибка.
    assert not any(item.attributes.get("langfuse.observation.level") == "ERROR" for item in items)


def test_deep_agent_keeps_outer_callbacks(monkeypatch):
    seen = {}

    class Agent:
        def invoke(self, inputs, config):
            seen.update(config)
            return {"messages": [AIMessage(content="готово")]}

    monkeypatch.setattr("deepagents.create_deep_agent", lambda **kwargs: Agent())
    monkeypatch.setattr("langchain_e2b.E2BSandbox", lambda **kwargs: None)
    listing = SimpleNamespace(error="нет файлов", logs=None)
    sandbox = SimpleNamespace(run_code=lambda *args, **kwargs: listing)
    manager = SimpleNamespace(get=lambda *args: sandbox)
    settings = Settings(_env_file=None, openai_api_key="test-openai-key")
    outer = BaseCallbackHandler()
    RunnableLambda(
        lambda _: run_deep_analysis(settings, None, manager, {"run_id": "r", "owner": "o"}, "задача")
    ).invoke(None, config={"callbacks": [outer]})
    assert outer in seen["callbacks"].handlers
    assert any(isinstance(item, RootBudgetCallback) for item in seen["callbacks"].handlers)
    assert seen["recursion_limit"] == 24


def test_provider_error_text_does_not_leave(traced, monkeypatch):
    client, exporter = traced
    monkeypatch.setattr("langchain_openai.ChatOpenAI", partial(TracedChat, fail=True))
    _, run_id = start(client, "Обращение: тест")
    assert wait(client, run_id)["status"] == "failed"
    items = spans(client, exporter)
    assert any(item.attributes.get("langfuse.observation.status_message") == "ValueError" for item in items)
    assert PROVIDER_ERROR not in everything(items)
