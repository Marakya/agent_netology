"""Сценарий поддержки: RAG по регламентам → карточка publish_support_draft → заявка с подтверждением."""

from test_app import client, decide, settings, start, wait  # noqa: F401

DEFECT = (
    "Обращение: Купил наушники 12 дней назад. Правый перестал работать. "
    "Коробку выбросил. Можно вернуть? Покупал с доставкой в Екатеринбург."
)


def test_sample_import_is_idempotent_and_private(client):  # noqa: F811
    first = client.post("/api/support/sample")
    assert first.status_code == 200
    assert len(first.json()["documents"]) == 4
    second = client.post("/api/support/sample").json()
    assert [d["id"] for d in first.json()["documents"]] == [d["id"] for d in second["documents"]]
    assert len(client.get("/api/documents").json()) == 4
    assert len(client.app.state.services.db.all("SELECT * FROM document_inputs")) == 4
    # Другой пользователь не видит чужие регламенты: RAG пуст → только ручная проверка.
    client.headers["Authorization"] = "Bearer token-b"
    assert client.get("/api/documents").json() == []
    _, run = start(client, DEFECT)
    result = wait(client, run)["result"]
    assert result["support_case"]["status"] == "manual_review"
    assert result["citations"] == []


def test_support_card_cites_policies_then_edited_approved_ticket(client):  # noqa: F811
    client.post("/api/support/sample")
    thread, run = start(client, DEFECT)
    result = wait(client, run)
    case = result["result"]["support_case"]
    assert result["status"] == "completed", result
    assert case["status"] == "needs_details"
    cited = {c["source_id"] for c in result["result"]["citations"]}
    # В citations только те источники, на которые ссылается карточка.
    assert case["source_ids"] and set(case["source_ids"]) == cited
    assert all(c["title"].startswith("Лист — ") for c in result["result"]["citations"])
    assert case["draft"] in result["result"]["answer"]
    # Карточка только предлагает заявку — без подтверждения ничего не создаётся.
    assert client.get("/api/tickets").json() == []
    proposal = case["ticket"]
    _, ticket_run = start(client, "/ticket " + proposal["title"] + "\n" + proposal["body"], thread)
    pending = wait(client, ticket_run)
    assert pending["status"] == "waiting_approval"
    assert client.get("/api/tickets").json() == []
    args = dict(pending["approvals"][0]["args"])
    args["body"] += "\nМенеджер уточнил номер: LS-2048."
    assert decide(client, pending, "edit", args)["status"] == "completed"
    tickets = client.get("/api/tickets").json()
    assert len(tickets) == 1 and "LS-2048" in tickets[0]["body"]
    client.headers["Authorization"] = "Bearer token-b"
    assert client.get("/api/tickets").json() == []


def test_rejected_ticket_has_no_effect(client):  # noqa: F811
    client.post("/api/support/sample")
    thread, run = start(client, "Обращение: Заказ LS-1042 задерживается. Где посылка?")
    case = wait(client, run)["result"]["support_case"]
    _, run = start(client, "/ticket " + case["ticket"]["body"], thread)
    assert decide(client, wait(client, run), "reject")["status"] == "completed"
    assert client.get("/api/tickets").json() == []


def test_deleted_policy_is_not_cited(client):  # noqa: F811
    docs = client.post("/api/support/sample").json()["documents"]
    for doc in docs:
        assert client.delete("/api/documents/" + doc["id"]).status_code == 200
    _, run = start(client, DEFECT)
    result = wait(client, run)["result"]
    assert result["support_case"]["status"] == "manual_review"
    assert result["citations"] == []
