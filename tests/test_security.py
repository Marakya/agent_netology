import socket
from unittest.mock import patch

import pytest

from app.security import public_url, redact, safe_external_query, workspace_path


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fd00::1", "192.168.0.1"])
def test_private_address_rejected(ip):
    with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]):
        with pytest.raises(ValueError):
            public_url("https://example.com/x")


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "https://user:pass@example.com", "http://example.com:8080"]
)
def test_bad_url(url):
    with pytest.raises(ValueError):
        public_url(url)


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "/workspace/../secret", "../../secret", "/workspace/a\\b", "/workspace/x\x00"]
)
def test_path_boundary(path):
    with pytest.raises(ValueError):
        workspace_path(path)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/workspace/", "/workspace"),
        ("/workspace", "/workspace"),
        ("/workspace/out/a.csv", "/workspace/out/a.csv"),
    ],
)
def test_workspace_root_and_children_allowed(path, expected):
    # FileOp по умолчанию использует "/workspace/" для ls — корень должен проходить проверку.
    assert workspace_path(path) == expected


@pytest.mark.parametrize("path", ["/workspacex/a", "workspace/a", "/tmp/workspace/a"])
def test_workspace_prefix_tricks_rejected(path):
    with pytest.raises(ValueError):
        workspace_path(path)


def test_redaction():
    assert "a@example.com" not in redact("email a@example.com")
    with pytest.raises(ValueError):
        safe_external_query("password=secret123")


def test_public_dns_prefers_ipv4_without_unpinning():
    records = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
    ]
    with patch("socket.getaddrinfo", return_value=records):
        _, addresses = public_url("https://example.com")
    assert addresses == ["1.1.1.1", "2606:4700:4700::1111"]
