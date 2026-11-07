import hashlib
import http.client
import mimetypes
import socket
import ssl
import threading
import time
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlunsplit

import httpx
import truststore

from .db import uid
from .security import public_url, safe_external_query, workspace_path


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip, self.parts = 0, []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.skip = max(0, self.skip - 1)
        if tag in {"p", "div", "li", "br"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def fetch_public(url):
    """Validate all DNS answers, then connect to that exact IP (no second DNS lookup)."""
    deadline = time.monotonic() + 20
    for _ in range(4):
        parts, addresses = public_url(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        host = parts.hostname

        class PinnedConnection(http.client.HTTPConnection):
            def connect(self):
                last_error = None
                raw = None
                for address in addresses[:4]:
                    try:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Fetch deadline exceeded")
                        raw = socket.create_connection((address, port), timeout=min(5, remaining))
                        break
                    except OSError as exc:
                        last_error = exc
                if raw is None:
                    raise last_error or OSError("No reachable public address")
                self.sock = (
                    truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT).wrap_socket(raw, server_hostname=host)
                    if parts.scheme == "https"
                    else raw
                )

        conn = PinnedConnection(host, port=port, timeout=10)
        try:
            conn.request(
                "GET",
                urlunsplit(("", "", parts.path or "/", parts.query, "")),
                headers={"User-Agent": "AgentNetology/0.1", "Accept-Encoding": "identity"},
            )
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Redirect без адреса")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f"Страница вернула HTTP {response.status}")
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise ValueError("Сжатый ответ не поддерживается безопасным fetch")
            content_type = response.getheader("Content-Type", "")
            if not any(t in content_type for t in ("text/", "application/json")):
                raise ValueError("Fetch поддерживает только текстовые страницы")
            chunks, size = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("Fetch deadline exceeded")
                chunk = response.read1(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise ValueError("Страница превышает 2 MiB")
                chunks.append(chunk)
            body = b"".join(chunks)
            text = body.decode("utf-8", errors="replace")
            if "html" in content_type:
                parser = TextExtractor()
                parser.feed(text)
                text = " ".join(parser.parts)
            return {
                "source_id": "web_" + uid(),
                "kind": "web",
                "title": host,
                "url": url,
                "locator": url,
                "excerpt": text[:16000],
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "content_hash": "sha256:" + hashlib.sha256(body).hexdigest(),
            }
        finally:
            conn.close()
    raise ValueError("Слишком много redirect")


def search_web(settings, query):
    if not settings.tavily_api_key.get_secret_value():
        raise ValueError("Интернет-поиск не подключён: задайте TAVILY_API_KEY")
    response = httpx.post(
        "https://api.tavily.com/search",
        headers={"Authorization": "Bearer " + settings.tavily_api_key.get_secret_value()},
        json={"query": safe_external_query(query), "max_results": 5, "search_depth": "basic"},
        timeout=20,
    )
    response.raise_for_status()
    return [
        {"title": r.get("title", ""), "url": r["url"], "snippet": r.get("content", "")[:2000]}
        for r in response.json().get("results", [])[:5]
    ]


class ArtifactStore:
    def __init__(self, settings, db):
        self.root, self.db = settings.data_dir / "artifacts", db

    def save(self, owner, run_id, name, data):
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("Артефакт превышает 20 MiB")
        artifact_id = uid()
        path = self.root / artifact_id
        path.write_bytes(data)
        name = PurePosixPath(name.replace("\\", "/")).name[:200] or "artifact.txt"
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.db.execute(
            "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)",
            (
                artifact_id,
                owner,
                run_id,
                name,
                str(path),
                mime,
                len(data),
                hashlib.sha256(data).hexdigest(),
                time.time(),
            ),
        )
        return {
            "artifact_id": artifact_id,
            "filename": name,
            "mime_type": mime,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }


class SandboxManager:
    def __init__(self, settings, db, artifacts):
        self.settings, self.db, self.artifacts = settings, db, artifacts
        self.lock, self.instances = threading.RLock(), {}

    def get(self, run_id, owner):
        if not self.settings.e2b_api_key.get_secret_value():
            raise ValueError("Sandbox не подключён: задайте E2B_API_KEY. Код на хосте не выполняется.")
        with self.lock:
            if run_id not in self.instances:
                from e2b_code_interpreter import Sandbox

                sbx = Sandbox.create(
                    api_key=self.settings.e2b_api_key.get_secret_value(),
                    timeout=600,
                    allow_internet_access=False,
                )
                self.instances[run_id] = sbx
                sbx.commands.run("mkdir -p /workspace/output /workspace/input", timeout=10)
                for row in self.db.all("SELECT * FROM artifacts WHERE owner=? AND run_id IS NULL", (owner,))[
                    -10:
                ]:
                    from pathlib import Path

                    sbx.files.write(
                        f"/workspace/input/{row['id']}_{row['name']}", Path(row["path"]).read_bytes()
                    )
                self.db.event(
                    run_id, "sandbox.created", {"id": sbx.sandbox_id, "network": "disabled", "ttl": 600}
                )
            return self.instances[run_id]

    def python(self, run_id, owner, code):
        sbx = self.get(run_id, owner)
        execution = sbx.run_code(code, timeout=60)
        result = {
            "stdout": "\n".join(execution.logs.stdout)[:100000],
            "stderr": "\n".join(execution.logs.stderr)[:10000],
            "error": str(execution.error)[:2000] if execution.error else None,
        }
        return result

    def files(self, run_id, owner, operation, path, content="", pattern="", offset=0, limit=100):
        path = workspace_path(path)
        # All path checks execute inside the remote sandbox too, preventing symlink escape.
        program = """
import pathlib, json, glob as glob_module
p = pathlib.Path(PATH).resolve()
root = pathlib.Path('/workspace').resolve()
if not p.is_relative_to(root): raise ValueError('path outside workspace')
if OP == 'write':
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(CONTENT, encoding='utf-8')
    print('written')
elif OP == 'read':
    if p.stat().st_size > 2*1024*1024: raise ValueError('file too large')
    lines=p.read_text(encoding='utf-8').splitlines()
    print(json.dumps({'lines': [f'{i+OFFSET+1}: {x}' for i,x in enumerate(lines[OFFSET:OFFSET+LIMIT])], 'truncated':len(lines)>OFFSET+LIMIT}))
elif OP == 'ls':
    print(json.dumps([str(x) for x in list(p.iterdir())[:200]]))
elif OP in ('glob','grep'):
    results=[]
    for i,x in enumerate(p.rglob('*')):
        if i>3000 or len(results)>=200: break
        if not x.resolve().is_relative_to(root): continue
        if OP=='glob' and x.match(PATTERN): results.append(str(x))
        if OP=='grep' and x.is_file() and x.stat().st_size<1000000:
            for line_no,line in enumerate(x.read_text(errors='replace').splitlines(),1):
                if PATTERN in line: results.append(f'{x}:{line_no}: {line[:500]}')
                if len(results)>=200: break
    print(json.dumps(results))
"""
        prefix = "\n".join(
            f"{k} = {v!r}"
            for k, v in dict(
                PATH=path, OP=operation, CONTENT=content, PATTERN=pattern, OFFSET=offset, LIMIT=limit
            ).items()
        )
        return self.python(run_id, owner, prefix + "\n" + program)

    def export(self, run_id, owner, path):
        path = workspace_path(path)
        sbx = self.get(run_id, owner)
        # Check no symlink and bound size before downloading. The run is serialized.
        result = sbx.run_code(
            "from pathlib import Path\np=Path(" + repr(path) + ")\n"
            "assert p.resolve().is_relative_to(Path('/workspace')) and not p.is_symlink()\n"
            "assert p.stat().st_size <= 20*1024*1024\n",
            timeout=10,
        )
        if result.error:
            raise ValueError("Небезопасный или слишком большой путь артефакта")
        data = sbx.files.read(path, format="bytes")
        return self.artifacts.save(owner, run_id, PurePosixPath(path).name, bytes(data))

    def close_run(self, run_id):
        with self.lock:
            sbx = self.instances.pop(run_id, None)
        if sbx:
            try:
                sbx.kill()
            except Exception:
                self.db.event(run_id, "sandbox.cleanup_pending", {"ttl_seconds": 600})

    def close(self):
        for run_id in list(self.instances):
            self.close_run(run_id)
