"""Unit-тесты Engine: ретраи провайдера, бюджет токенов, сжатие контекста, метрики инструментов."""

import time

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from pydantic import SecretStr

from app.config import Settings
from app.engine import cited, is_transient
from app.main import create_app


@pytest.fixture
def services(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, openai_api_key="test-openai-key")
    with TestClient(create_app(settings)) as client:
        yield client.app.state.services


def make_run(services, run_id="run1"):
    # Завершённый run: Engine его не подхватит, но события и бюджет можно писать.
    now = time.time()
    services.db.execute("INSERT INTO threads VALUES(?,?,?,?)", ("t1", "u", "t", now))
    services.db.execute(
        "INSERT INTO runs(id,thread_id,owner,message,status,created) VALUES(?,?,?,?,?,?)",
        (run_id, "t1", "u", "m", "completed", now),
    )
    return run_id


def timeout_error():
    return openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/chat"))


def test_transient_classification():
    assert is_transient(timeout_error())
    assert is_transient(RuntimeError(), status=429)
    assert is_transient(httpx.ConnectTimeout("x"))
    assert not is_transient(ValueError("bad args"))
    assert not is_transient(RuntimeError(), status=400)


def test_timeout_is_retried_and_failed_reservation_refunded(services, monkeypatch):
    run_id = make_run(services)
    attempts = []

    class FlakyChat:
        def __init__(self, **kwargs):
            pass

        def invoke(self, messages):
            attempts.append(1)
            if len(attempts) == 1:
                raise timeout_error()
            return AIMessage(
                content="ok", usage_metadata={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}
            )

    monkeypatch.setattr("langchain_openai.ChatOpenAI", FlakyChat)
    response = services.engine.chat([HumanMessage(content="hi")], run_id)
    assert response.content == "ok"
    assert len(attempts) == 2
    row = services.db.one("SELECT model_calls,tokens FROM runs WHERE id=?", (run_id,))
    # Обе попытки учитываются как вызовы, но токены — только фактические успешного вызова.
    assert row == {"model_calls": 2, "tokens": 10}


def test_context_compaction_removes_old_messages_once(services):
    run_id = make_run(services)
    filler = "x" * 4000
    history = []
    for turn in range(4):
        history += [
            HumanMessage(content=f"вопрос {turn} {filler}", id=f"h{turn}"),
            AIMessage(content=f"ответ {turn} {filler}", id=f"a{turn}"),
        ]
    state = {"messages": history, "run_id": run_id, "owner": "u", "summary": "старое резюме"}

    summary, removals = services.engine.compact(state)

    # Сохраняются два последних хода, остальное уходит в summary (вызов LLM) и удаляется из состояния.
    assert [m.id for m in removals] == ["h0", "a0", "h1", "a1"]
    assert all(isinstance(m, RemoveMessage) for m in removals)
    assert summary == "Краткое резюме прошлых ходов."  # ответ тестовой LLM из conftest
    assert services.db.one("SELECT model_calls FROM runs WHERE id=?", (run_id,))["model_calls"] == 1

    # После применения removals повторного сжатия (и повторного вызова LLM) нет.
    state["messages"], state["summary"] = history[4:], summary
    assert services.engine.compact(state) == (summary, [])
    prompt = services.engine.prompt(state)
    assert [m.id for m in prompt[1:]] == ["h2", "a2", "h3", "a3"]
    assert summary in prompt[0].text


def test_sandbox_success_is_logged_as_ok(services, monkeypatch):
    run_id = make_run(services)
    # files доступен только при наличии sandbox; сам sandbox подменяем — облако не вызывается.
    services.settings.e2b_api_key = SecretStr("test")
    # Успешный ответ sandbox содержит ключ error со значением None.
    monkeypatch.setattr(services.tools.sandbox, "files", lambda *a, **k: {"stdout": "[]", "error": None})
    state = {
        "run_id": run_id,
        "owner": "u",
        "sources": [],
    }
    update = services.engine.execute_tool(state, {"name": "files", "args": {"operation": "ls"}, "id": "c1"})
    assert isinstance(update["messages"][0], ToolMessage)
    assert update["limitations"] == []
    event = services.db.one("SELECT payload FROM events WHERE run_id=? AND type='tool.completed'", (run_id,))
    assert '"ok": true' in event["payload"]


def test_citations_keep_only_referenced_sources():
    sources = [{"source_id": "a"}, {"source_id": "b"}]
    assert cited(sources, ["b"]) == [{"source_id": "b"}]  # карточка: явные source_ids
    assert cited(sources, "Факт [a].") == [{"source_id": "a"}]  # текст: метки [source_id]
    assert cited(sources, "Без меток") == sources  # меток нет — показываем всё найденное
    assert cited(sources, []) == []  # manual_review без источников
