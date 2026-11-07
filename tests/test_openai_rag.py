"""Real OpenAI SDK serialization + real persistent Qdrant; HTTP responses are mocked."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from test_live_protocol import wait

from app.config import Settings
from app.main import create_app


def test_openai_embeddings_tools_and_support_card(tmp_path, monkeypatch):
    requests = []
    chat_count = 0

    def respond(request):
        nonlocal chat_count
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": body["model"],
                    "data": [
                        {"object": "embedding", "index": i, "embedding": [1.0] + [0.0] * 7}
                        for i in range(len(body["input"]))
                    ],
                    "usage": {"prompt_tokens": 10, "total_tokens": 10},
                },
            )
        assert request.url.path.endswith("/chat/completions")
        chat_count += 1
        message = {"role": "assistant", "content": "Черновик готов."}
        if chat_count == 1:
            name, args = "rag_search", {"query": "проверка неисправного товара"}
        elif chat_count == 2:
            assert body["messages"][-1]["content"].startswith("{"), body["messages"][-1]
            evidence = json.loads(body["messages"][-1]["content"])["sources"]
            name, args = (
                "publish_support_draft",
                {
                    "category": "Неисправность",
                    "draft": "Здравствуйте! Примем обращение на проверку.",
                    "questions": ["Какой номер заказа?"],
                    "reason": "Согласно регламенту магазина.",
                    "assignee": "Менеджер качества",
                    "status": "needs_details",
                    "source_ids": [evidence[0]["source_id"]],
                },
            )
        else:
            name = None
            if chat_count == 4:
                message["content"] = "Здравствуйте! Зарегистрируем обращение на проверку качества."
        if chat_count <= 2:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call-{chat_count}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "id": f"chat-{chat_count}",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if chat_count <= 2 else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr("langchain_openai.ChatOpenAI", lambda **kw: ChatOpenAI(http_client=http, **kw))
        monkeypatch.setattr(
            "langchain_openai.OpenAIEmbeddings", lambda **kw: OpenAIEmbeddings(http_client=http, **kw)
        )
        settings = Settings(
            _env_file=None,
            data_dir=tmp_path,
            openai_api_key="fake-test-key",
            embedding_dimensions=8,
        )
        with TestClient(create_app(settings)) as client:
            uploaded = client.post(
                "/api/documents",
                files={"file": ("my-store.md", "Неисправный товар принимаем на проверку качества.".encode())},
            )
            assert uploaded.status_code == 201, uploaded.text
            config = client.get("/api/config").json()
            assert "publish_support_draft" in config["tools"]
            assert config["embedding_dimensions"] == 8
            thread = client.post("/api/threads", json={}).json()["id"]
            run = client.post(
                f"/api/threads/{thread}/runs",
                json={"message": "Наушники не работают"},
                headers={"Idempotency-Key": "openai-rag"},
            ).json()["id"]
            result = wait(client, run)
            assert result["status"] == "completed", result
            case = result["result"]["support_case"]
            assert case["draft"] in result["result"]["answer"]
            assert case["draft"] in case["ticket"]["body"]
            assert result["result"]["citations"][0]["document_id"] == uploaded.json()["id"]
            assert client.get("/api/tickets").json() == []
            assert result["model_calls"] == 4 and result["tool_calls"] == 2
    embeddings = [body for path, body in requests if path.endswith("/embeddings")]
    assert len(embeddings) == 2  # ingestion and retrieval both use OpenAI embeddings
    assert all(body["model"] == "text-embedding-3-small" and body["dimensions"] == 8 for body in embeddings)
    chats = [body for path, body in requests if path.endswith("/chat/completions")]
    assert chats[0]["model"] == "gpt-4.1-mini"
    offered = {t["function"]["name"] for t in chats[0]["tools"]}
    assert "rag_search" in offered and "publish_support_draft" in offered
    # wrap_model_call не предлагает модели инструменты без ключей (Tavily, E2B).
    assert not offered & {"web_search", "run_python", "execute", "deep_analyze"}
    assert chats[0]["parallel_tool_calls"] is False


def test_support_card_rejects_unknown_evidence_and_ungrounded_status(tmp_path):
    with TestClient(
        create_app(Settings(_env_file=None, data_dir=tmp_path, openai_api_key="test-openai-key"))
    ) as client:
        tools = client.app.state.services.tools
        args = {
            "category": "Возврат",
            "draft": "Передам менеджеру",
            "questions": [],
            "reason": "Нет правил",
            "assignee": "Менеджер",
            "status": "needs_details",
            "source_ids": [],
        }
        context = {"run_id": "test", "owner": "local-user", "sources": []}
        with pytest.raises(ValueError, match="manual_review"):
            tools.execute("publish_support_draft", args, context)
        args["status"] = "manual_review"
        args["source_ids"] = ["invented-source"]
        with pytest.raises(ValueError, match="Источник"):
            tools.execute("publish_support_draft", args, context)
        args["source_ids"] = []
        assert (
            tools.execute("publish_support_draft", args, context)["support_case"]["status"] == "manual_review"
        )
