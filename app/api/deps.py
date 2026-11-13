"""Общие зависимости роутеров: доступ к сервисам, авторизация и проверки владельца."""

import hmac
import json

from fastapi import Depends, Header, HTTPException, Request

LOCAL_OWNER = "local-user"  # владелец при работе без токена или с единственным APP_TOKEN
ACTIVE_STATUSES = ("queued", "running", "waiting_approval")
# Таблицы, в которых есть колонка owner и которые разрешено читать через owned().
OWNED_TABLES = {"threads", "runs", "approvals", "artifacts", "memories"}


def services(request: Request):
    """Контейнер сервисов приложения (создаётся в main.Services при старте)."""
    return request.app.state.services


def principal(request: Request, s=Depends(services), authorization: str = Header(default="")):
    """Определить владельца запроса. Возвращает имя владельца или отвечает 401/403.

    Режимы по убыванию приоритета:
    1. APP_USERS_JSON — несколько пользователей, у каждого свой токен;
    2. APP_TOKEN — один пользователь;
    3. без токенов — доступ только с localhost и только с того же origin.
    """
    token = authorization.removeprefix("Bearer ")
    raw_users = s.settings.app_users_json.get_secret_value()
    if raw_users:
        for expected, owner in json.loads(raw_users).items():
            # compare_digest: сравнение за постоянное время, чтобы токен нельзя было подобрать по таймингу.
            if hmac.compare_digest(token, expected):
                return str(owner)
        raise HTTPException(401, "Нужен действующий токен доступа")
    expected = s.settings.app_token.get_secret_value()
    if expected:
        if hmac.compare_digest(token, expected):
            return LOCAL_OWNER
        raise HTTPException(401, "Нужен токен APP_TOKEN")
    # testserver / testclient — адреса тестового клиента FastAPI.
    host = request.url.hostname
    if host not in {"localhost", "127.0.0.1", "::1", "testserver"} or request.client.host not in {
        "127.0.0.1",
        "::1",
        "testclient",
    }:
        raise HTTPException(403, "Без токена доступ разрешён только с localhost")
    # Защита от запросов с чужих сайтов (CSRF): Origin должен совпадать с адресом приложения.
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Cross-origin запрос запрещён")
    return LOCAL_OWNER


def owned(s, table, object_id, owner):
    """Вернуть объект владельца или 404. Чужой объект неотличим от несуществующего."""
    if table not in OWNED_TABLES:
        raise ValueError("Unknown table")
    row = s.db.one(f"SELECT * FROM {table} WHERE id=? AND owner=?", (object_id, owner))
    if not row:
        raise HTTPException(404, "Объект не найден")
    return row


def assert_idle(s, owner):
    """Запретить изменение данных (документы, память, беседы), пока у владельца выполняется задача."""
    if s.db.one(
        "SELECT id FROM runs WHERE owner=? AND status IN ('queued','running','waiting_approval')",
        (owner,),
    ):
        raise HTTPException(409, "Сначала завершите или отмените активную задачу")
    # Статус уже финальный, но поток ещё не вышел из Engine.work (закрывает песочницу и т.п.).
    with s.engine.lock:
        for run_id in s.engine.active:
            if s.db.one("SELECT id FROM runs WHERE id=? AND owner=?", (run_id, owner)):
                raise HTTPException(409, "Задача завершает сохранение. Повторите через секунду")


def reset_context(s, owner):
    """Удалить checkpoint всех бесед владельца.

    Нужно после удаления памяти или документа: иначе старый результат инструмента в истории
    модели «вернул бы» удалённые данные. Видимая история сообщений остаётся.
    """
    for thread in s.db.all("SELECT id FROM threads WHERE owner=?", (owner,)):
        s.engine.saver.delete_thread(thread["id"])
