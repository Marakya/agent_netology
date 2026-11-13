# Реализованная версия и целевая архитектура

Streamlit: `streamlit_app.py` предоставляет чат, документы, память, источники, карточки и подтверждения. `run-streamlit.ps1` запускает UI с внутренним API; при AGENT_API_URL используется внешний backend. Общая бизнес-логика и база не дублируются. Порт UI — 8501; Docker-профиль — `streamlit`.

Текущий продуктовый сценарий — помощник поддержки магазина «Лист» ([spec.md](../spec.md#3-сценарий-работы)): четыре учебных документа, черновики ответов от LLM по RAG, уточнения и локальные заявки с подтверждением. Режима без модели нет.

README и spec.md описывают запускаемый код. Остальные проектные документы сохраняют более широкий production target.

OpenAI (4 ноября 2026): `rag_search` использует OpenAI embeddings и Qdrant; модель формирует карточку через `publish_support_draft`. Источники проверяются по текущей задаче и владельцу, review обновляет черновик и предложение заявки согласованно. Настройки и отдельный Docker Qdrant: [openai-qdrant.md](openai-qdrant.md).

| Тема презентации | Код | Поведение |
|---|---|---|
| Агент | app/langchain_agent.py | create_agent, ApplicationMiddleware (before_model / wrap_model_call / after_model), StructuredTool + ToolRuntime |
| Модели/prompts/structured output | Engine.call_model, app/models.py | ChatOpenAI tools, static/dynamic, fallback, Pydantic envelope |
| Память/checkpoints | app/db.py, app/engine.py | SqliteSaver, cross-thread memory, версии и optimistic concurrency, owner из auth |
| Trim/summary/context editing | Engine.compact, Engine.prompt | Полные human turns, live summary, сокращение старых results |
| State/Tools | Engine.execute_tool/tool_update | ToolMessage, state updates, validated registry |
| Middleware/hooks | ApplicationMiddleware, app/deep.py | Хуки LangChain вокруг модели; Deep Agents использует штатные middleware |
| HITL | Engine.execute_tool, API approvals | interrupt/Command resume, approve/edit/reject, restart |
| Limits | Database.reserve/reserve_tool | Root counters, shared budget, per-tool quota, timeout |
| PII/errors/retries | app/security.py, Engine.dlp/call_model | Redaction, secret detection, transient model retry |
| Todo/selector | write_todos, Toolbox.available + wrap_model_call | Todo events, фильтр инструментов по подключённым ключам |
| Files/shell | SandboxManager, app/tools.py | Реальные E2B files и execution, без host fallback |
| Subagents | Engine.specialist | Изолированный контекст, scope tools, bounded loop |
| LangGraph | Engine.graph | StateGraph agent → finish, checkpoints, interrupt, events/recovery |
| Deep Agents | app/deep.py | create_deep_agent + E2BSandbox, встроенные files/todo/context middleware |
| Sandbox | SandboxManager | Network off, timeout, provider TTL, cleanup и artifact export |
| Qdrant RAG | app/rag.py | Embedded/server Qdrant, dense+sparse RRF, owner/canonical filters |
| Интернет | app/providers.py | Tavily и DNS-pinned fetch |
| SQL/интеграции | Toolbox.tool_query_dataset / tool_create_ticket | Parameterized SELECT registry, local tickets с approval |

Завершение run записывает result, видимое сообщение, закрытие approvals и финальное событие в одной SQLite-транзакции. Финальный checkpoint сохраняется раньше этой транзакции: при восстановлении готовый ответ используется без нового вызова модели. После отмены незавершённые tool calls исключаются из контекста следующего хода. Это не гарантирует exactly-once выполнение внешних sandbox-команд при аварии провайдера.

Штатные LLMToolSelector, LLMToolEmulator и погодный API не активированы: инструменты фильтруются по конфигурации в wrap_model_call. Deep Agents добавляет свои middleware, приложение не дублирует их.

Production extensions: PostgreSQL/PostgresStore, S3, OIDC/RBAC, distributed leases, memory expiry/full deletion jobs, reranker/document versions, OCR, outbox для внешних CRM, distributed quotas, dashboards и load benchmarks. Текущий runtime — один API-процесс, локальный durable ledger и owner isolation.

Cloud smoke требует ключей. Проверены локальные integration сценарии и SDK signatures: E2B create(allow_internet_access=False), run_code(timeout), E2BSandbox(workdir, timeout). Наличие adapter не означает проверку аккаунта/тарифа. Облачные CPU/RAM/disk limits требуют provider template; код не заявляет их измеренными.
