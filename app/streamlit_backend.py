"""API внутри процесса Streamlit: один сервер на все сессии, данные сессий не кэшируются."""

import atexit
import os
import socket
import threading
import time

import httpx
import streamlit as st
import uvicorn

from .config import Settings
from .main import create_app


class LocalBackend:
    """Запускает FastAPI в фоновом потоке на случайном порту localhost."""

    def __init__(self):
        settings = Settings()
        address = st.get_option("server.address")
        if address not in {"127.0.0.1", "localhost", "::1"} and not (
            settings.app_token.get_secret_value() or settings.app_users_json.get_secret_value()
        ):
            raise ValueError("Для сетевого Streamlit задайте APP_TOKEN или APP_USERS_JSON")
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                self.close()
                raise RuntimeError("Не удалось запустить API. Проверьте .env и блокировку DATA_DIR.")
            time.sleep(0.05)
        atexit.register(self.close)

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.socket.close()


@st.cache_resource(show_spinner="Запускаем агента и базу знаний…")
def local_backend():
    return LocalBackend()


def backend_url():
    """Адрес API: внешний из AGENT_API_URL либо встроенный сервер."""
    return os.environ.get("AGENT_API_URL", "").rstrip("/") or local_backend().url


class ApiError(Exception):
    pass


def request_api(base_url, token, method, path, **kwargs):
    """HTTP-запрос к API. Любая ошибка превращается в ApiError с понятным текстом."""
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        response = httpx.request(
            method, base_url + "/api" + path, headers=headers, timeout=180, follow_redirects=False, **kwargs
        )
    except httpx.HTTPError as exc:
        raise ApiError("API недоступен. Проверьте запуск сервера и подключение.") from exc
    if not response.is_success:
        try:
            detail = response.json().get("detail", "Ошибка запроса")
        except ValueError:
            detail = "Ошибка запроса"
        raise ApiError(f"HTTP {response.status_code}: {detail}")
    return response
