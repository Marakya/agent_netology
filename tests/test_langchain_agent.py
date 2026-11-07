"""Exercise create_agent routing and durable approvals without paid API calls."""

import json

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from test_app import decide, start, wait

from app.config import Settings
from app.main import create_app


def setup_provider(monkeypatch):
    class Embeddings:
        def __init__(self, **kwargs):
            pass

        def embed_documents(self, texts):
            return [[1.0] + [0.0] * 7 for _ in texts]

        def embed_query(self, text):
            return [1.0] + [0.0] * 7

    class Model:
        def __init__(self, **kwargs):
            pass

        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, messages):
            last = messages[-1]
            if isinstance(last, ToolMessage):
                data = json.loads(last.content)
                if "documents" not in data:
                    return AIMessage(content="Done")
                assert [d["name"] for d in data["documents"]] == ["policy.txt"]
                name, args = "create_ticket", {"title": "Test ticket", "body": "Needs review"}
            else:
                name, args = "list_knowledge_documents", {}
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])

    monkeypatch.setattr("langchain_openai.ChatOpenAI", Model)
    monkeypatch.setattr("langchain_openai.OpenAIEmbeddings", Embeddings)


def test_agent_inventory_approval_restart_and_followup(tmp_path, monkeypatch):
    setup_provider(monkeypatch)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        openai_api_key="fake-test-key",
        embedding_dimensions=8,
        app_users_json='{"a":"alice","b":"bob"}',
    )
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer b"
        client.post("/api/documents", files={"file": ("secret.txt", b"Bob's policy")})
        client.headers["Authorization"] = "Bearer a"
        client.post("/api/documents", files={"file": ("policy.txt", b"Alice's policy")})
        engine = client.app.state.services.engine
        assert "agent" in engine.graph.get_graph().nodes
        thread, run_id = start(client, "List policies and propose a ticket")
        pending = wait(client, run_id)
        assert pending["status"] == "waiting_approval", pending
        assert client.get("/api/tickets").json() == []
    # Nested create_agent checkpoint must resume without replaying side effects.
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer a"
        result = decide(client, pending)
        assert result["status"] == "completed", result
        assert len(client.get("/api/tickets").json()) == 1
        events = client.get(f"/api/runs/{run_id}/log").json()["events"]
        started = [e["details"]["name"] for e in events if e["event"] == "tool.started"]
        assert started == ["list_knowledge_documents", "create_ticket"]
        _, next_id = start(client, "Propose another ticket", thread=thread)
        pending = wait(client, next_id)
        assert pending["status"] == "waiting_approval", pending
        rejected = decide(client, pending, decision="reject")
        assert rejected["status"] == "completed", rejected
        assert len(client.get("/api/tickets").json()) == 1
