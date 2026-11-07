import json

from test_app import client, decide, settings, start, wait  # noqa: F401


def test_journal_metadata_file_owner_and_limits(client):  # noqa: F811
    _, run_id = start(client, "/ticket private-customer-text")
    pending = wait(client, run_id)
    assert pending["status"] == "waiting_approval"
    decide(client, pending)
    response = client.get(f"/api/runs/{run_id}/log")
    assert response.status_code == 200
    data = response.json()
    types = [e["event"] for e in data["events"]]
    assert types[0] == "run.queued"
    assert "approval.required" in types and "approval.decided" in types
    assert "tool.started" in types and "tool.completed" in types
    assert types[-1] == "run.completed"
    assert "private-customer-text" not in response.text
    limited = client.get(f"/api/runs/{run_id}/log?limit=2").json()
    assert len(limited["events"]) == 2 and limited["truncated"]
    assert client.get(f"/api/runs/{run_id}/log?limit=9999").status_code == 422
    log = client.app.state.services.settings.data_dir / "logs" / "agent.jsonl"
    contents = log.read_text(encoding="utf-8")
    assert "private-customer-text" not in contents
    entries = [json.loads(line) for line in contents.splitlines()]
    assert any(e["run_id"] == run_id and e["event"] == "run.completed" for e in entries)
    client.headers["Authorization"] = "Bearer token-b"
    assert client.get(f"/api/runs/{run_id}/log").status_code == 404


def test_log_excludes_sensitive_payloads_and_rotates(client):  # noqa: F811
    _, run_id = start(client, "привет")
    wait(client, run_id)
    db = client.app.state.services.db
    db.action_log.handler.maxBytes = 500
    for _ in range(12):
        db.event(
            run_id,
            "model.failed",
            {
                "model": "gpt-4.1-mini",
                "error_type": "AuthenticationError",
                "http_status": 401,
                "answer": "private answer",
                "query": "alice@example.org",
                "args": {"token": "secret-value"},
                "api_key": "sk-sensitive-value-12345",
                "reasoning": "private reasoning",
            },
        )
    data = client.get(f"/api/runs/{run_id}/log").json()
    last = data["events"][-1]
    assert last["level"] == "ERROR" and last["details"]["http_status"] == 401
    files = list((client.app.state.services.settings.data_dir / "logs").glob("agent.jsonl*"))
    assert 2 <= len(files) <= 4
    content = json.dumps(data) + "".join(p.read_text(encoding="utf-8") for p in files)
    for value in ["private answer", "alice@example.org", "secret-value", "sk-sensitive", "private reasoning"]:
        assert value not in content


def test_engine_error_is_logged_without_exception_body(client, monkeypatch):  # noqa: F811
    def fail(*args):
        raise RuntimeError("sk-sensitive-value-12345 private-prompt")

    monkeypatch.setattr(client.app.state.services.engine, "chat_model", fail)
    _, run_id = start(client, "привет")
    assert wait(client, run_id)["status"] == "failed"
    data = client.get(f"/api/runs/{run_id}/log").json()
    failed = next(e for e in data["events"] if e["event"] == "run.failed")
    assert failed["details"]["error_type"] == "RuntimeError"
    assert "private-prompt" not in json.dumps(data)
    assert "sk-sensitive" not in json.dumps(data)
