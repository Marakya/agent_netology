import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        openai_api_key="test-openai-key",
        app_users_json='{"token-a":"alice","token-b":"bob"}',
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        c.headers["Authorization"] = "Bearer token-a"
        yield c


def start(client, message, thread=None, key=None):
    thread = thread or client.post("/api/threads", json={"title": "test"}).json()["id"]
    response = client.post(
        f"/api/threads/{thread}/runs",
        json={"message": message},
        headers={"Idempotency-Key": key or str(time.time_ns())},
    )
    assert response.status_code == 202, response.text
    return thread, response.json()["id"]


def wait(client, run_id, target=None):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = client.get("/api/runs/" + run_id).json()
        if result["status"] in (
            target or {"completed", "partial", "failed", "cancelled", "waiting_approval"}
        ):
            return result
        time.sleep(0.03)
    pytest.fail(f"Run stuck: {result}")


def decide(client, run, decision="approve", edited_args=None):
    a = run["approvals"][0]
    body = {"decision": decision, "action_hash": a["hash"]}
    if edited_args is not None:
        body["edited_args"] = edited_args
    response = client.post("/api/approvals/" + a["id"], json=body)
    assert response.status_code == 200, response.text
    return wait(client, run["id"])


def test_chat_idempotency_and_history(client):
    thread, run_id = start(client, "привет", key="same")
    result = wait(client, run_id)
    assert result["status"] == "completed", result
    response = client.post(
        f"/api/threads/{thread}/runs", json={"message": "привет"}, headers={"Idempotency-Key": "same"}
    )
    assert response.json()["id"] == run_id
    response = client.post(
        f"/api/threads/{thread}/runs", json={"message": "другое"}, headers={"Idempotency-Key": "same"}
    )
    assert response.status_code == 409
    assert len(client.get("/api/threads/" + thread).json()["messages"]) == 2


def test_memory_approval_edit_and_cross_thread(client):
    _, run_id = start(client, "запомни язык: русский")
    result = wait(client, run_id)
    assert result["status"] == "waiting_approval", result
    assert client.get("/api/memory").json() == []
    result = decide(client, result, "edit", {"key": "язык", "value": "English"})
    assert result["status"] == "completed", result
    _, next_id = start(client, "/memory")
    assert "English" in wait(client, next_id)["result"]["answer"]
    memory_id = client.get("/api/memory").json()[0]["id"]
    assert client.delete("/api/memory/" + memory_id).status_code == 200
    assert client.get("/api/memory").json() == []


def test_reject_and_idempotent_ticket(client):
    _, run_id = start(client, "/ticket Review delivery")
    result = wait(client, run_id)
    approved = decide(client, result)
    assert approved["status"] == "completed", approved
    assert len(client.app.state.services.db.all("SELECT * FROM tickets")) == 1
    decide(client, result)
    assert len(client.app.state.services.db.all("SELECT * FROM tickets")) == 1
    _, other_id = start(client, "запомни секретное_правило: ничего")
    result = decide(client, wait(client, other_id), "reject")
    assert result["status"] == "completed"
    assert client.get("/api/memory").json() == []


def test_qdrant_ingestion_citations_and_owner_filter(client):
    text = "Регламент: доставка товара занимает 7 рабочих дней. Ответственный отдел логистики."
    response = client.post("/api/documents", files={"file": ("policy.txt", text.encode(), "text/plain")})
    assert response.status_code == 201, response.text
    document = response.json()
    duplicate = client.post("/api/documents", files={"file": ("policy.txt", text.encode(), "text/plain")})
    assert duplicate.json()["id"] == document["id"]
    _, run_id = start(client, "Какие сроки доставки товара?")
    result = wait(client, run_id)
    assert result["status"] == "completed", result
    assert result["result"]["citations"][0]["document_id"] == document["id"]
    assert "7 рабочих дней" in result["result"]["answer"]
    client.headers["Authorization"] = "Bearer token-b"
    assert client.get("/api/documents").json() == []
    assert client.get("/api/runs/" + run_id).status_code == 404
    assert client.app.state.services.rag.search("bob", "доставка") == []
    client.headers["Authorization"] = "Bearer token-a"
    assert client.delete("/api/documents/" + document["id"]).status_code == 200
    assert client.app.state.services.rag.search("alice", "доставка") == []


def test_restart_pending_approval(settings):
    with TestClient(create_app(settings)) as first:
        first.headers["Authorization"] = "Bearer token-a"
        thread, run_id = start(first, "запомни формат: кратко")
        assert wait(first, run_id)["status"] == "waiting_approval"
    with TestClient(create_app(settings)) as second:
        second.headers["Authorization"] = "Bearer token-a"
        result = decide(second, second.get("/api/runs/" + run_id).json())
        assert result["status"] == "completed", result
        assert second.get("/api/memory").json()[0]["value"] == "кратко"
        assert len(second.get("/api/threads/" + thread).json()["messages"]) == 2


def test_artifacts_download_auth_and_thread_delete(client):
    thread, run_id = start(client, "/report # Report\nEvidence goes here")
    result = wait(client, run_id)
    assert result["status"] == "completed", result
    artifact_id = result["result"]["artifacts"][0]["artifact_id"]
    assert b"# Report" in client.get("/api/artifacts/" + artifact_id).content
    client.headers["Authorization"] = "Bearer token-b"
    assert client.get("/api/artifacts/" + artifact_id).status_code == 404
    client.headers["Authorization"] = "Bearer token-a"
    assert client.delete("/api/threads/" + thread).status_code == 200
    assert client.get("/api/artifacts/" + artifact_id).status_code == 404


def test_cancel_waiting_and_unavailable_sandbox(client):
    _, run_id = start(client, "запомни ключ: значение")
    assert wait(client, run_id)["status"] == "waiting_approval"
    assert client.post("/api/runs/" + run_id + "/cancel").status_code == 200
    assert wait(client, run_id)["status"] == "cancelled"
    _, run_id = start(client, "/python print(2+2)")
    result = wait(client, run_id)
    assert result["status"] == "partial", result
    assert "не подключён" in result["result"]["answer"]


def test_token_required_and_approval_owner(client):
    _, run_id = start(client, "запомни язык: русский")
    result = wait(client, run_id)
    a = result["approvals"][0]
    client.headers["Authorization"] = "Bearer token-b"
    assert (
        client.post(
            "/api/approvals/" + a["id"], json={"decision": "approve", "action_hash": a["hash"]}
        ).status_code
        == 404
    )
    client.headers.pop("Authorization")
    assert client.get("/api/threads").status_code == 401


def test_sse_and_budget(client):
    _, run_id = start(client, "привет")
    wait(client, run_id)
    response = client.get("/api/runs/" + run_id + "/events")
    assert "event: run.completed" in response.text
    from app.db import BudgetExceeded

    db = client.app.state.services.db
    with pytest.raises(BudgetExceeded):
        db.reserve(run_id, "model", 1)


def test_memory_versions_conflicts_and_history_owner(client):
    first = client.post("/api/memory", json={"key": "language", "value": "ru"}).json()
    memory_id = first["id"]
    assert first["version"] == 1
    update = {"key": "language", "value": "en", "expected_version": 1}
    assert client.post("/api/memory", json=update).json()["version"] == 2
    assert client.post("/api/memory", json=update).status_code == 409
    history = client.get(f"/api/memory/{memory_id}/history").json()
    assert [r["version"] for r in history] == [2, 1]
    client.headers["Authorization"] = "Bearer token-b"
    assert client.get(f"/api/memory/{memory_id}/history").status_code == 404
    client.headers["Authorization"] = "Bearer token-a"
    assert client.delete(f"/api/memory/{memory_id}").status_code == 200
    assert client.app.state.services.db.all("SELECT * FROM memory_revisions") == []


def test_stale_memory_approval_cannot_overwrite(client):
    client.post("/api/memory", json={"key": "формат", "value": "первый"})
    _, run_id = start(client, "запомни формат: второй")
    pending = wait(client, run_id)
    assert pending["approvals"][0]["args"]["expected_version"] == 1
    assert (
        client.post(
            "/api/memory",
            json={
                "key": "формат",
                "value": "третий",
                "expected_version": 1,
            },
        ).status_code
        == 200
    )
    result = decide(client, pending)
    assert result["status"] == "partial", result
    assert client.get("/api/memory").json()[0]["value"] == "третий"


def test_cancel_then_new_turn_has_valid_tool_history(client):
    from langchain_core.messages import AIMessage, ToolMessage

    thread, run_id = start(client, "запомни язык: русский")
    wait(client, run_id)
    client.post(f"/api/runs/{run_id}/cancel")
    assert wait(client, run_id)["status"] == "cancelled"
    _, next_id = start(client, "привет", thread=thread)
    assert wait(client, next_id)["status"] == "completed"
    engine = client.app.state.services.engine
    messages = engine.graph.get_state(engine.config(thread)).values["messages"]
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    assert all(c["id"] in answered for m in messages if isinstance(m, AIMessage) for c in m.tool_calls)
    assert client.get("/api/memory").json() == []
    assert (
        client.app.state.services.db.one("SELECT status FROM approvals WHERE run_id=?", (run_id,))["status"]
        == "closed"
    )


def test_restart_after_final_checkpoint_does_not_repeat_model(settings):
    with TestClient(create_app(settings)) as first:
        first.headers["Authorization"] = "Bearer token-a"
        engine = first.app.state.services.engine
        engine.complete = lambda *args: None  # Simulate loss before application commit.
        thread, run_id = start(first, "привет")
        deadline = time.monotonic() + 15
        while run_id in engine.active and time.monotonic() < deadline:
            time.sleep(0.03)
        assert run_id not in engine.active
        snapshot = engine.graph.get_state(engine.config(thread))
        assert not snapshot.next and snapshot.values["answer"]
        calls = first.get("/api/runs/" + run_id).json()["model_calls"]
    with TestClient(create_app(settings)) as second:
        second.headers["Authorization"] = "Bearer token-a"
        result = wait(second, run_id)
        assert result["status"] == "completed"
        assert result["model_calls"] == calls
        from app.models import Answer

        second.app.state.services.engine.complete(run_id, Answer(answer="duplicate", status="completed"))
        assert len(second.get("/api/threads/" + thread).json()["messages"]) == 2
        assert second.get(f"/api/runs/{run_id}/events").text.count("event: run.completed") == 1


def test_memory_mutation_replay(client):
    db = client.app.state.services.db
    db.save_memory("alice", "format", "short", "test", mutation_id="effect-1")
    db.save_memory("alice", "format", "long", "test", expected_version=1, mutation_id="effect-2")
    replay = db.save_memory("alice", "format", "short", "test", mutation_id="effect-1")
    assert replay["version"] == 1
    assert db.memory("alice")[0]["value"] == "long"
    assert len(db.all("SELECT * FROM memory_revisions")) == 2


def test_cancel_at_interrupt_boundary(client, monkeypatch):
    engine = client.app.state.services.engine
    invoke = engine.graph.invoke

    def cancel_before_waiting(inputs, config):
        result = invoke(inputs, config=config)
        if result.get("__interrupt__"):
            engine.db.execute("UPDATE runs SET cancel=1 WHERE id=?", (inputs["run_id"],))
        return result

    monkeypatch.setattr(engine.graph, "invoke", cancel_before_waiting)
    _, run_id = start(client, "запомни язык: русский")
    assert wait(client, run_id)["status"] == "cancelled"
    assert client.get("/api/memory").json() == []
