"""Protocol-level tests: exercise live branches without consuming cloud credentials."""

import time

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from app.config import Settings
from app.main import create_app


def wait(client, run_id):
    for _ in range(300):
        result = client.get("/api/runs/" + run_id).json()
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(0.02)
    raise AssertionError("run stuck")


def test_live_tool_call_and_reviewer(tmp_path, monkeypatch):
    calls = []

    class FakeChat:
        def __init__(self, **kwargs):
            self.tools = None

        def bind_tools(self, tools, **kwargs):
            self.tools = tools
            return self

        def invoke(self, messages):
            calls.append(messages)
            usage = {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20}
            if self.tools and not isinstance(messages[-1], ToolMessage):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "rag_search",
                            "args": {"query": "доставка"},
                            "id": "call1",
                            "type": "tool_call",
                        }
                    ],
                    usage_metadata=usage,
                )
            return AIMessage(content="Доставка занимает 7 рабочих дней.", usage_metadata=usage)

    monkeypatch.setattr("langchain_openai.ChatOpenAI", FakeChat)
    settings = Settings(_env_file=None, data_dir=tmp_path, openai_api_key="test-openai-key")
    with TestClient(create_app(settings)) as client:
        client.post(
            "/api/documents",
            files={"file": ("policy.txt", "Доставка занимает 7 рабочих дней.".encode(), "text/plain")},
        )
        thread = client.post("/api/threads", json={}).json()["id"]
        run_id = client.post(
            f"/api/threads/{thread}/runs",
            json={"message": "Доставка? email me at a@example.com"},
            headers={"Idempotency-Key": "live"},
        ).json()["id"]
        result = wait(client, run_id)
        assert result["status"] == "completed", result
        assert result["model_calls"] == 3
        assert result["tokens"] == 60
        assert result["result"]["citations"]
        assert all("a@example.com" not in str(m.content) for batch in calls for m in batch)


def test_deep_budget_callback_blocks_nested_task(tmp_path):
    import pytest

    from app.db import BudgetExceeded, Database
    from app.deep import RootBudgetCallback

    settings = Settings(_env_file=None, data_dir=tmp_path, max_tool_calls=1)
    db = Database(tmp_path / "test.sqlite")
    db.execute("INSERT INTO threads VALUES(?,?,?,?)", ("t", "u", "test", time.time()))
    db.execute(
        "INSERT INTO runs(id,thread_id,owner,status,created,started_at) VALUES(?,?,?,?,?,?)",
        ("r", "t", "u", "running", time.time(), time.time()),
    )
    callback = RootBudgetCallback(settings, db, "r")
    with pytest.raises(PermissionError):
        callback.on_tool_start({"name": "task"}, "{}", run_id="c1")
    callback.on_tool_start({"name": "read_file"}, "{}", run_id="c2")
    with pytest.raises(BudgetExceeded):
        callback.on_tool_start({"name": "execute"}, "{}", run_id="c3")
    db.close()


def test_sandbox_adapter_network_and_cleanup(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.db import Database
    from app.providers import ArtifactStore, SandboxManager

    calls = []

    class FakeSandbox:
        sandbox_id = "remote-test"
        commands = SimpleNamespace(run=lambda *a, **k: None)

        @classmethod
        def create(cls, **kwargs):
            calls.append(kwargs)
            return cls()

        def run_code(self, code, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(logs=SimpleNamespace(stdout=["343"], stderr=[]), error=None)

        def kill(self):
            calls.append("killed")

    monkeypatch.setattr("e2b_code_interpreter.Sandbox", FakeSandbox)
    settings = Settings(
        _env_file=None, data_dir=tmp_path, e2b_api_key="fake", openai_api_key="test-openai-key"
    )
    settings.prepare()
    db = Database(tmp_path / "test.sqlite")
    db.execute("INSERT INTO threads VALUES(?,?,?,?)", ("t", "u", "test", time.time()))
    db.execute(
        "INSERT INTO runs(id,thread_id,owner,status,created) VALUES(?,?,?,?,?)",
        ("r", "t", "u", "running", time.time()),
    )
    manager = SandboxManager(settings, db, ArtifactStore(settings, db))
    assert manager.python("r", "u", "print(7**3)")["stdout"] == "343"
    assert calls[0]["allow_internet_access"] is False
    assert calls[1]["timeout"] == 60
    manager.close_run("r")
    assert calls[-1] == "killed"
    db.close()
