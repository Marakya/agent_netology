import ipaddress
import re
import socket
from pathlib import PurePosixPath
from urllib.parse import urlsplit

SECRET = re.compile(r"(?i)(?:sk-[a-z0-9_-]{12,}|(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+)")
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")


def redact(text: str) -> str:
    return EMAIL.sub("[EMAIL]", SECRET.sub("[SECRET]", text))


def safe_external_query(text: str) -> str:
    if SECRET.search(text):
        raise ValueError("Нельзя отправлять секреты внешнему поисковику")
    return EMAIL.sub("[EMAIL]", text)[:2000]


def public_url(url: str):
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
    # Prefer IPv4 on machines without an IPv6 route; all resolved IPs remain validated.
    return parts, sorted(addresses, key=lambda ip: (ipaddress.ip_address(ip).version, ip))


def workspace_path(path: str) -> str:
    if "\x00" in path or "\\" in path or ".." in PurePosixPath(path).parts:
        raise ValueError("Недопустимый путь")
    p = PurePosixPath(path)
    # Сам корень "/workspace/" допустим (например, для ls); str(PurePosixPath) убирает хвостовой слеш.
    if not p.is_relative_to("/workspace"):
        raise ValueError("Путь должен находиться в /workspace/")
    return str(p)
