# Покрытие презентации

Источник: `C:/documents/проект_УРФУ/agent_netology/agents (1).pptx`, 66 слайдов. Текст сохранён в [slides-extracted.txt](slides-extracted.txt). Схемы на слайдах 3, 24, 38 и 46 просмотрены отдельно как изображения. Таблица показывает **проектное покрытие**, не статус реализации. Axx — будущие приёмочные проверки из [implementation.md](implementation.md).

Обозначения: ARCH — [architecture.md](architecture.md), MEM — [memory-rag.md](memory-rag.md), TOOL — [tools-security.md](tools-security.md), OPS — [api-operations.md](api-operations.md).

| Слайд | Возможность | Решение | Проверка |
|---|---|---|---|
| 1 | Разработка агентов | ARCH, назначение проекта | A01 |
| 2 | Все темы курса | Полный набор документов и эта матрица | A01–A32 |
| 3 | LLM, память, tools, выбор сценария | ARCH, router и agent loop; пример аренды обобщён в routing | A01, A32 |
| 4 | Компоненты агента | ARCH, компоненты | A01 |
| 5 | Static/dynamic model | ARCH, model registry и routing | A32 |
| 6 | Стоимость и tracking модели | OPS, usage и trace model_id | A26, A32 |
| 7 | Search, вычисления, файлы, SQL, интеграции | TOOL, каталог | A14, A17, A29, A31 |
| 8 | Один/несколько @tool, пример погоды | TOOL, registry и optional WeatherProvider | A30 |
| 9 | Dynamic tools по состоянию/ролям | ARCH, authorized catalog и selector; gateway | A04, A30 |
| 10 | Tool error handling | TOOL, sanitized ToolResult | A23 |
| 11 | Выбор и выполнение tools, локальная LLM | ARCH, model adapters, execution gateway | A32 |
| 12 | Static/dynamic system prompt | ARCH, версионированные prompts | A30, A32 |
| 13 | Structured output, Pydantic, ToolStrategy | ARCH, final validator; JSON Schema | A01, A23 |
| 14 | Short-term, thread_id, InMemorySaver | MEM, state и test profile | A03, A08 |
| 15 | Postgres/SQLite saver, custom state | MEM, durable checkpoint и context | A03 |
| 16 | Long-term Store | MEM, canonical MemoryStoreAdapter | A05 |
| 17 | Ограничение контекста | MEM, token budget | A08 |
| 18 | Trim в before_model | MEM, trim целых turns | A08 |
| 19 | trim_messages и границы истории | MEM, tool pair integrity | A08 |
| 20 | RemoveMessage и очистка | MEM, active state против полного удаления | A07, A08 |
| 21 | Summarization | MEM, summary с provenance | A08 |
| 22 | Чтение state через ToolRuntime | MEM, trusted runtime context | A04, A30 |
| 23 | Command update и ToolMessage | MEM, allowlist state updates | A30 |
| 24 | История + embedding/vector data | MEM, отдельные checkpoint/KB/artifacts; Qdrant вместо примерного PVS | A05, A09 |
| 25 | Prompt из памяти/context | ARCH/MEM, bounded recall и dynamic prompt | A05 |
| 26 | Middleware lifecycle | ARCH, таблица hooks | A30 |
| 27 | SummarizationMiddleware | MEM, trigger и сохранение последних turns | A08 |
| 28 | Context editing, ClearToolUses | MEM, offloading старых tool bodies | A08 |
| 29 | HITL approve/edit/reject | OPS, approval protocol | A20 |
| 30 | Persistent interrupt/resume | OPS, durable approval, same checkpoint | A21 |
| 31 | Model/tool call limits | OPS, shared root ledger и per-tool quota | A19 |
| 32 | Fallback, PII | ARCH/TOOL, compatible fallback и DLP | A23, A25 |
| 33 | Todo и LLMToolSelector | ARCH, explicit todo и authorized subset | A19, A32 |
| 34 | Tool/model retries | OPS, bounded retry и reconciliation | A22, A23 |
| 35 | LLM tool emulator | ARCH, simulation only, synthetic flag | A26 |
| 36 | Shell и file search | TOOL, sandbox execute; host policy не используется в сервисе | A15, A29 |
| 37 | FilesystemMiddleware | TOOL, sandbox file tools без дублирования middleware | A29 |
| 38 | Backend → generated tools → wrappers | ARCH/TOOL, provider adapter и async gateway | A29, A30 |
| 39 | ls | TOOL, виртуальные абсолютные пути | A29 |
| 40 | read_file, pagination, limits | TOOL, offset/limit и truncated | A29 |
| 41 | write_file | TOOL, создание и expected_hash | A29 |
| 42 | edit_file | TOOL, unique old_string и read-before-edit | A29 |
| 43 | glob | TOOL, bounded wildcard search | A29 |
| 44 | grep, literal pattern и modes | TOOL, literal grep, три режима | A29 |
| 45 | Subagents | ARCH, task contract, контекст и общий бюджет | A19 |
| 46 | Простой/продвинутый agent loop | ARCH, ветки и lifecycle diagram | A01, A30 |
| 47 | Node/wrap hooks | ARCH, порядок и ответственность | A30 |
| 48 | Hooks, retries, jumps | ARCH, ограниченные переходы | A23, A30 |
| 49 | Decorator middleware | ARCH, единичные hooks | A30 |
| 50 | Полный список decorators | ARCH, before/after/wrap/dynamic_prompt | A30 |
| 51 | Class middleware | ARCH, async stateful policies | A30 |
| 52 | Custom state, counters | MEM, state projection и durable ledger | A19 |
| 53 | Порядок hooks, jump_to | ARCH, order contract | A30 |
| 54 | Dynamic model и tool monitoring | ARCH/OPS, routing и sanitized traces | A26, A32 |
| 55 | LangGraph, StateGraph, interrupt | ARCH/OPS, explicit graph и resume | A03, A21 |
| 56 | Deep Agents, task/files/memory/skills/todo | ARCH, Analyst subgraph и reviewed skills | A17, A19, A29 |
| 57 | Стоимость, сложность, безопасность Deep Agents | ARCH, simple fast path и restricted complex profile | A19, A25 |
| 58 | Sandbox, backend и изоляция | TOOL, remote environment boundary | A15 |
| 59 | Sandbox providers | TOOL, adapter; альтернативы в sources.md | A15, A18 |
| 60 | Deep Agents + E2B | TOOL, E2B adapter и backend lifecycle | A17, A18 |
| 61 | Замена Daytona/Modal | TOOL, provider protocol, compatibility gates | A18 |
| 62 | Sandbox через @tool в create_agent | TOOL, run_python/execute wrappers | A15, A17 |
| 63 | Upload/download, thread lifetime, TTL | TOOL, S3 export, sandbox generation/janitor | A17, A18 |
| 64 | Ресурсы, сеть, secrets, approval | TOOL/OPS, deny egress, quotas, HITL | A15, A16, A20 |
| 65 | Filesystem/LocalShell не sandbox | TOOL, явный запрет host execution для LLM | A15 |
| 66 | Различие backend/sandbox и выбор | ARCH/TOOL, remote default и test fakes | A15, A26 |

Дополнительно по запросу пользователя: Qdrant hybrid RAG, ingestion/versioning, ACL post-filter, citation validator и отдельная память. Не требуется одновременно подключать всех sandbox-провайдеров или все внешние CRM: покрыта возможность замены и описаны проверяемые контракты. Код из презентации — иллюстрация механизма, не обязательная неизменная реализация.
