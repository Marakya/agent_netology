"""Интерфейс Streamlit. Вся логика — в API (app/api); здесь только отображение и вызовы HTTP."""

import json
import uuid

import streamlit as st

from app.streamlit_backend import ApiError, backend_url, request_api

st.set_page_config(page_title="Лист — поддержка магазина", page_icon="🌿", layout="wide")


def api(method, path, **kwargs):
    return request_api(st.session_state.api_url, st.session_state.get("token", ""), method, path, **kwargs)


def get(path):
    return api("GET", path).json()


def start(message):
    """Отправить сообщение агенту: при необходимости создать беседу и запустить задачу."""
    thread_id = st.session_state.get("thread_id")
    if not thread_id:
        thread_id = api("POST", "/threads", json={"title": message[:80]}).json()["id"]
        st.session_state.thread_id = thread_id
    # Ключ идемпотентности хранится до успешного ответа: повтор после сетевой ошибки не создаст дубль.
    pending = st.session_state.get("pending")
    if not pending or pending["message"] != message or pending["thread_id"] != thread_id:
        pending = {"message": message, "thread_id": thread_id, "key": uuid.uuid4().hex}
        st.session_state.pending = pending
    api(
        "POST",
        f"/threads/{thread_id}/runs",
        json={"message": message},
        headers={"Idempotency-Key": pending["key"]},
    )
    st.session_state.pop("pending", None)
    st.rerun()


def render_result(run):
    """Итог задачи: карточка с редактируемым черновиком, источники, файлы, ограничения."""
    result = run.get("result") or {}
    case = result.get("support_case")
    if case:
        st.subheader("Черновик ответа")
        st.caption(case["reason"])
        draft = st.text_area(
            "Проверьте и отредактируйте ответ", case["draft"], key="draft_" + run["id"], height=180
        )
        st.download_button("Скачать ответ", draft, file_name="reply.txt", mime="text/plain")
        if st.button("Подготовить заявку", key="ticket_" + run["id"]):
            body = case["ticket"]["body"] + "\nЧерновик после проверки менеджером:\n" + draft
            start("/ticket " + case["ticket"]["title"] + "\n" + body[:4700])
        st.caption("Ответ не отправляется покупателю. Заявка требует отдельного подтверждения.")
    for source in result.get("citations", []):
        with st.expander(source["title"] + " · " + source["locator"]):
            st.text(source["excerpt"])
            st.caption(source["source_id"])
    for item in result.get("artifacts", []):
        if st.button("Подготовить файл: " + item["filename"], key="fetch_" + item["artifact_id"]):
            data = api("GET", "/artifacts/" + item["artifact_id"]).content
            st.download_button(
                "Скачать " + item["filename"],
                data,
                file_name=item["filename"],
                key="download_" + item["artifact_id"],
            )
    for limitation in result.get("limitations", []):
        st.caption(limitation)


def render_approval(run):
    """Форма решения человека по действию агента: подтвердить, изменить аргументы или отклонить."""
    for approval in run["approvals"]:
        st.warning("Подтвердите действие: " + approval["tool"])
        with st.form("approval_" + approval["id"]):
            edited = st.text_area(
                "Аргументы действия (JSON)",
                json.dumps(approval["args"], ensure_ascii=False, indent=2),
                height=220,
            )
            choice = st.radio("Решение", ["Подтвердить", "Изменить и выполнить", "Отклонить"])
            if st.form_submit_button("Применить решение"):
                decision = {"Подтвердить": "approve", "Изменить и выполнить": "edit", "Отклонить": "reject"}[
                    choice
                ]
                body = {"decision": decision, "action_hash": approval["hash"]}
                if decision == "edit":
                    try:
                        args = json.loads(edited)
                        if not isinstance(args, dict):
                            raise ValueError()
                    except ValueError:
                        st.error("Введите корректный JSON-объект.")
                        return
                    body["edited_args"] = args
                api("POST", "/approvals/" + approval["id"], json=body)
                st.rerun()


def render_log(run_id):
    """Журнал действий агента по задаче."""
    journal = get(f"/runs/{run_id}/log")
    with st.expander("Журнал действий агента", expanded=True):
        st.caption(f"Задача: {run_id} · время UTC · событий: {journal['total']}")
        rows = [
            {
                "Время (UTC)": event["time"],
                "Уровень": event["level"],
                "Действие": event["action"],
                "Подробности": json.dumps(event["details"], ensure_ascii=False),
            }
            for event in journal["events"]
        ]
        if rows:
            st.dataframe(rows, hide_index=True, use_container_width=True)
        else:
            st.caption("События ещё не записаны.")
        if journal["truncated"]:
            st.caption("Показаны последние 200 событий; полная история хранится на сервере.")
        st.download_button(
            "Скачать журнал JSON",
            json.dumps(journal, ensure_ascii=False, indent=2),
            file_name=f"agent-{run_id}.json",
            mime="application/json",
            key="log_" + run_id,
        )


@st.fragment(run_every="1s")
def progress(run_id):
    """Фрагмент, который раз в секунду обновляет состояние выполняющейся задачи."""
    try:
        run = get("/runs/" + run_id)
        st.info(
            f"Агент работает · модель: {run['model_calls']} · tools: {run['tool_calls']} · "
            f"токены: {run['tokens']}"
        )
        render_log(run_id)
        if run["status"] not in {"queued", "running"}:
            st.rerun()
        if st.button("Остановить задачу", key="stop_" + run_id):
            api("POST", f"/runs/{run_id}/cancel")
            st.rerun()
    except ApiError as exc:
        st.error(str(exc))


def main():
    st.title("🌿 Лист — помощник поддержки")
    st.caption("Обращение → правила магазина → черновик → подтверждённая заявка")
    st.session_state.api_url = backend_url()
    with st.sidebar:
        with st.form("access"):
            token = st.text_input(
                "Токен доступа (APP_TOKEN)", type="password", value=st.session_state.get("token", "")
            )
            if st.form_submit_button("Подключиться"):
                # При смене пользователя сбрасываем всё состояние сессии: черновики, выбранную беседу.
                st.session_state.clear()
                st.session_state.token = token
                st.rerun()
        st.caption("Локально без APP_TOKEN оставьте поле пустым. Ключ OpenAI задаётся в .env на сервере.")
    config = get("/config")
    with st.sidebar:
        st.caption(f"Модель: {config['model']} · Qdrant: {config['qdrant']}")
        st.caption("Embeddings: " + config["embedding_model"])
        if st.button("Новое обращение"):
            st.session_state.pop("thread_id", None)
            st.session_state.pop("pending", None)
            st.rerun()
        for thread in get("/threads"):
            if st.button(thread["title"], key="thread_" + thread["id"]):
                st.session_state.thread_id = thread["id"]
                st.rerun()
        st.button("Обновить")

    chat, documents, memory, tickets = st.tabs(["Обращение", "База знаний", "Память", "Заявки"])
    with documents:
        st.subheader("Документы магазина")
        if st.button("Загрузить пример магазина"):
            with st.spinner("Индексируем четыре документа…"):
                api("POST", "/support/sample")
            st.success("Учебный магазин загружен.")
        with st.form("upload"):
            uploaded = st.file_uploader(
                "Регламент или каталог, до 10 MiB", type=["txt", "md", "csv", "pdf", "docx", "pptx"]
            )
            if st.form_submit_button("Добавить документ") and uploaded:
                with st.spinner("Создаём embeddings и индексируем в Qdrant…"):
                    api("POST", "/documents", files={"file": (uploaded.name, uploaded.getvalue())})
                st.success("Документ добавлен.")
        for doc in get("/documents"):
            st.write(f"{doc['name']} · {doc['chunks']} фрагм. · {doc['state']}")
            if st.button("Удалить документ", key="doc_" + doc["id"]):
                api("DELETE", "/documents/" + doc["id"])
                st.rerun()
    with memory:
        st.subheader("Подтверждённая память")
        for item in get("/memory"):
            with st.expander(item["key"]):
                with st.form("memory_" + item["id"]):
                    value = st.text_area("Значение", item["value"])
                    if st.form_submit_button("Сохранить изменение"):
                        api(
                            "POST",
                            "/memory",
                            json={"key": item["key"], "value": value, "expected_version": item["version"]},
                        )
                        st.rerun()
                if st.button("История", key="hist_" + item["id"]):
                    st.json(get("/memory/" + item["id"] + "/history"))
                if st.button("Забыть", key="forget_" + item["id"]):
                    api("DELETE", "/memory/" + item["id"])
                    st.rerun()
        with st.form("new_memory"):
            key = st.text_input("Название предпочтения")
            value = st.text_area("Значение предпочтения")
            if st.form_submit_button("Запомнить"):
                api("POST", "/memory", json={"key": key, "value": value, "expected_version": 0})
                st.rerun()
    with tickets:
        st.caption("Локальный реестр. Записи не отправляются в CRM и сохраняются при удалении беседы.")
        rows = get("/tickets")
        if not rows:
            st.info("Подтверждённых заявок пока нет.")
        for ticket in rows:
            with st.expander(ticket["title"]):
                st.caption(ticket["id"])
                st.text(ticket["body"])
    with chat:
        thread_id = st.session_state.get("thread_id")
        thread = get("/threads/" + thread_id) if thread_id else {"messages": [], "runs": []}
        run = get("/runs/" + thread["runs"][-1]["id"]) if thread["runs"] else None
        busy = run and run["status"] in {"queued", "running", "waiting_approval"}
        if not thread["messages"]:
            st.info("Загрузите документы во вкладке «База знаний», затем вставьте обращение покупателя.")
            if st.button("Пример: неисправные наушники", disabled=bool(busy)):
                start(
                    "Обращение: Купил наушники 12 дней назад. Правый перестал работать. "
                    "Коробку выбросил. Можно вернуть?"
                )
        for message in thread["messages"]:
            with st.chat_message(message["role"]):
                st.text(message["content"])
        if run:
            if run["status"] in {"queued", "running"}:
                progress(run["id"])
            elif run["status"] == "waiting_approval":
                render_log(run["id"])
                render_approval(run)
                if st.button("Отменить задачу"):
                    api("POST", f"/runs/{run['id']}/cancel")
                    st.rerun()
            else:
                render_log(run["id"])
                st.caption(f"Статус: {run['status']} · tokens: {run['tokens']}")
                render_result(run)
        if len(thread["runs"]) > 1:
            with st.expander("Журналы предыдущих шагов"):
                previous = list(reversed(thread["runs"][:-1]))
                selected = st.selectbox(
                    "Выберите задачу", [item["id"] for item in previous], key="history_log_" + thread_id
                )
                render_log(selected)
        pending = st.session_state.get("pending")
        if pending and pending["thread_id"] == thread_id and not busy:
            if st.button("Повторить отправку после ошибки"):
                start(pending["message"])
        if message := st.chat_input("Обращение покупателя или команда", disabled=bool(busy)):
            start(message)
        if thread_id and st.checkbox("Разрешить удаление этой беседы", key="delete_" + thread_id):
            if st.button("Удалить беседу", disabled=bool(busy)):
                api("DELETE", "/threads/" + thread_id)
                st.session_state.pop("thread_id", None)
                st.rerun()


try:
    main()
except (ApiError, RuntimeError, ValueError) as exc:
    st.error(str(exc))
