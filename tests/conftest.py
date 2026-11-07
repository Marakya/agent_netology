"""Общие тестовые заглушки провайдера.

Приложение всегда работает с настоящими ChatOpenAI и OpenAIEmbeddings. В тестах платный
API не вызывается: autouse-фикстура подменяет оба класса в langchain_openai на сценарные
заглушки с тем же интерфейсом (bind_tools/invoke, embed_documents/embed_query).
Тест может переопределить заглушку своим monkeypatch — он применяется позже autouse.
"""

import hashlib
import json
import math
import re

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

TEST_KEY = "test-openai-key"
USAGE = {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20}


class FakeEmbeddings:
    """Детерминированный bag-of-words: одинаковые слова → близкие векторы."""

    def __init__(self, dimensions=1536, **kwargs):
        self.dim = dimensions

    def embed_query(self, text):
        vec = [0.0] * self.dim
        for word in re.findall(r"\w+", text.lower()):
            vec[int(hashlib.blake2b(word.encode(), digest_size=4).hexdigest(), 16) % self.dim] += 1
        norm = math.sqrt(sum(v * v for v in vec)) or 1
        return [v / norm for v in vec]

    def embed_documents(self, texts):
        return [self.embed_query(t) for t in texts]


def call(name, args):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": "call_" + name, "type": "tool_call"}]
    )


def text(content):
    return AIMessage(content=content, usage_metadata=USAGE)


class ScriptedChat:
    """Сценарная «LLM» для тестов: выбирает инструмент по команде и отвечает по его результату."""

    def __init__(self, **kwargs):
        self.model, self.tools = kwargs.get("model"), None

    def bind_tools(self, tools, **kwargs):
        self.tools = tools
        return self

    def invoke(self, messages):
        if not self.tools:
            return self.plain(messages)
        human = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        request, turn = messages[human].text, messages[human + 1 :]
        if turn and isinstance(turn[-1], ToolMessage):
            return self.after_tool(request, turn, json.loads(turn[-1].content))
        message = self.choose(request)
        message.usage_metadata = USAGE
        return message

    @staticmethod
    def plain(messages):
        system = messages[0].text if isinstance(messages[0], SystemMessage) else ""
        if system.startswith("Ты reviewer"):
            return text(json.loads(messages[-1].text)["answer"])  # reviewer ничего не меняет
        return text("Краткое резюме прошлых ходов.")

    @staticmethod
    def choose(request):
        lower = request.lower()
        if lower.startswith("запомни "):
            key, _, value = request[8:].partition(":")
            return call("save_memory", {"key": key.strip(), "value": value.strip()})
        if lower.startswith("/ticket "):
            body = request[8:]
            return call("create_ticket", {"title": body.splitlines()[0][:200], "body": body[:5000]})
        if lower.startswith("/memory"):
            return call("read_memory", {})
        if lower.startswith("/report "):
            return call("save_report", {"filename": "report.md", "content": request[8:]})
        if lower.startswith("/python "):
            return call("run_python", {"code": request[8:]})
        if lower in {"привет", "hello"}:
            return AIMessage(content="Здравствуйте! Пришлите обращение покупателя.")
        return call("rag_search", {"query": request})

    @staticmethod
    def after_tool(request, turn, result):
        published = any(
            c["name"] == "publish_support_draft"
            for m in turn
            if isinstance(m, AIMessage)
            for c in m.tool_calls
        )
        if request.lower().startswith("обращение:") and not published:
            # Карточка поддержки строго по найденным источникам; без них — manual_review.
            ids = [s["source_id"] for s in result.get("sources", [])[:2]]
            return call(
                "publish_support_draft",
                {
                    "category": "Обращение покупателя",
                    "draft": "Здравствуйте! Передадим обращение ответственному. Уточните номер заказа.",
                    "questions": ["Уточните номер заказа."],
                    "reason": "Нужна проверка менеджером.",
                    "assignee": "старший менеджер",
                    "status": "needs_details" if ids else "manual_review",
                    "source_ids": ids,
                },
            )
        if result.get("ticket_id"):
            return text("Заявка зарегистрирована: " + result["ticket_id"])
        if result.get("sources"):
            return text("\n".join(f"{s['excerpt'][:300]} [{s['source_id']}]" for s in result["sources"]))
        return text("Результат инструмента: " + json.dumps(result, ensure_ascii=False))


@pytest.fixture(autouse=True)
def fake_openai(monkeypatch):
    monkeypatch.setattr("langchain_openai.ChatOpenAI", ScriptedChat)
    monkeypatch.setattr("langchain_openai.OpenAIEmbeddings", FakeEmbeddings)
