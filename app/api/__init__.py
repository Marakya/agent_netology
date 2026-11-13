"""REST API приложения. Каждый модуль — отдельный роутер FastAPI.

* deps.py      — общие зависимости: сервисы, авторизация, проверка владельца;
* threads.py   — беседы и запуск задач;
* runs.py      — состояние задачи, журнал, поток событий (SSE), отмена, подтверждения;
* knowledge.py — документы базы знаний;
* memory.py    — подтверждённая память владельца;
* system.py    — конфигурация, реестр заявок, скачивание артефактов.
"""

from . import knowledge, memory, runs, system, threads

ROUTERS = [system.router, threads.router, runs.router, knowledge.router, memory.router]
