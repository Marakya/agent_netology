"""Защитные проверки: маскирование данных (DLP), публичные URL (SSRF), пути песочницы."""

import ipaddress
import re
import socket
from pathlib import PurePosixPath
from urllib.parse import urlsplit

# Похоже на секрет: ключ вида sk-..., либо «api_key/password/secret/token = значение».
SECRET = re.compile(r"(?i)(?:sk-[a-z0-9_-]{12,}|(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+)")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")


def redact(text: str) -> str:
    """Заменить секреты и e-mail метками. Применяется перед отправкой текста в LLM и журналы."""
    return EMAIL.sub("[EMAIL]", SECRET.sub("[SECRET]", text))


def safe_external_query(text: str) -> str:
    """Подготовить запрос для внешнего поисковика: секреты запрещены, e-mail маскируются."""
    if SECRET.search(text):
        raise ValueError("Нельзя отправлять секреты внешнему поисковику")
    return EMAIL.sub("[EMAIL]", text)[:2000]


def public_url(url: str):
    """Проверить, что URL ведёт в публичный интернет (защита от SSRF).

    Возвращает разобранный URL и список проверенных IP-адресов: соединяться нужно именно
    с ними, без повторного DNS-запроса, иначе адрес можно подменить между проверкой и запросом.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Разрешены только публичные HTTP(S) URL без credentials")
    if parts.port not in (None, 80, 443):
        raise ValueError("Нестандартный порт запрещён")
    addresses = {
        x[4][0] for x in socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    }
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("Доступ к внутренним сетям запрещён")
    # IPv4 первыми: на машинах без IPv6-маршрута так быстрее. Проверены все адреса.
    return parts, sorted(addresses, key=lambda ip: (ipaddress.ip_address(ip).version, ip))


def workspace_path(path: str) -> str:
    """Проверить путь в песочнице: только внутри /workspace, без «..» и обратных слешей."""
    if "\x00" in path or "\\" in path or ".." in PurePosixPath(path).parts:
        raise ValueError("Недопустимый путь")
    p = PurePosixPath(path)
    # Сам корень "/workspace/" допустим (например, для ls); str(PurePosixPath) убирает хвостовой слеш.
    if not p.is_relative_to("/workspace"):
        raise ValueError("Путь должен находиться в /workspace/")
    return str(p)
