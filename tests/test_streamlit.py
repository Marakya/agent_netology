import time

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app.config import Settings
from app.main import create_app
from app.streamlit_backend import ApiError


@pytest.fixture
def ui(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        openai_api_key="test-openai-key",
        app_users_json='{"token-a":"alice","token-b":"bob"}',
    )
    with TestClient(create_app(settings)) as client:

        def request(base_url, token, method, path, **kwargs):
            headers = kwargs.pop("headers", {})
            headers["Authorization"] = "Bearer " + token
            response = client.request(method, "/api" + path, headers=headers, **kwargs)
            if not response.is_success:
                raise ApiError(f"HTTP {response.status_code}: {response.json()['detail']}")
            return response

        monkeypatch.setattr("app.streamlit_backend.backend_url", lambda: "http://local-test")
        monkeypatch.setattr("app.streamlit_backend.request_api", request)
        at = AppTest.from_file("../streamlit_app.py", default_timeout=30)
        at.session_state["token"] = "token-a"
        yield at, client


def button(at, label):
    return next(item for item in at.button if item.label == label)


def settle(at, client):
    thread = at.session_state["thread_id"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        rows = client.app.state.services.db.all("SELECT status FROM runs WHERE thread_id=?", (thread,))
        if rows and rows[-1]["status"] not in {"queued", "running"}:
            at.run()
            assert not at.exception
            return
        time.sleep(0.05)
    pytest.fail("Run did not settle")


def test_streamlit_support_and_approval(ui):
    at, client = ui
    at.run()
    assert not at.exception and not at.error
    button(at, "Загрузить пример магазина").click().run()
    assert not at.exception and not at.error
    assert len(client.app.state.services.db.all("SELECT * FROM documents")) == 4
    button(at, "Пример: неисправные наушники").click().run()
    settle(at, client)
    draft = next(x for x in at.text_area if x.label == "Проверьте и отредактируйте ответ")
    assert any(x.label == "Журнал действий агента" for x in at.expander)
    assert at.dataframe
    draft.set_value("Ответ проверен менеджером.").run()
    button(at, "Подготовить заявку").click().run()
    settle(at, client)
    assert not client.app.state.services.db.all("SELECT * FROM tickets")
    assert "Ответ проверен менеджером." in next(x.value for x in at.text_area if "JSON" in x.label)
    button(at, "Применить решение").click().run()
    settle(at, client)
    tickets = client.app.state.services.db.all("SELECT * FROM tickets")
    assert len(tickets) == 1
    assert "Ответ проверен менеджером." in tickets[0]["body"]
    assert not at.error


def test_streamlit_identity_change_and_unauthorized_access(ui):
    at, client = ui
    at.session_state["token"] = "invalid"
    at.run()
    assert at.error and not at.exception
    next(x for x in at.text_input if "APP_TOKEN" in x.label).set_value("token-a")
    button(at, "Подключиться").click().run()
    assert not at.error
    button(at, "Пример: неисправные наушники").click().run()
    settle(at, client)
    assert at.session_state["thread_id"]
    next(x for x in at.text_input if "APP_TOKEN" in x.label).set_value("token-b")
    button(at, "Подключиться").click().run()
    assert not at.exception and not at.error
    assert "thread_id" not in at.session_state
    assert not at.chat_message
    assert not any(x.label == "Проверьте и отредактируйте ответ" for x in at.text_area)


def test_embedded_backend_http_and_shutdown(tmp_path, monkeypatch):
    from app import streamlit_backend as backend

    settings = Settings(
        _env_file=None, data_dir=tmp_path, openai_api_key="test-openai-key", app_token="test-token"
    )
    monkeypatch.setattr(backend, "Settings", lambda: settings)
    monkeypatch.setattr(backend.st, "get_option", lambda key: "127.0.0.1")
    runtime = backend.LocalBackend()
    try:
        with pytest.raises(ApiError, match="401"):
            backend.request_api(runtime.url, "", "GET", "/config")
        response = backend.request_api(runtime.url, "test-token", "GET", "/config")
        assert response.json()["model"] == settings.model_fast
    finally:
        runtime.close()
    assert not runtime.thread.is_alive()
