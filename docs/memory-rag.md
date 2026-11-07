# Память и RAG

## Виды памяти

| Вид | Scope | Содержание и хранилище |
|---|---|---|
| Краткосрочная | tenant/user/thread | Messages, summary, plan, checkpoint в PostgreSQL; 30 дней после активности |
| Долгосрочная | tenant/user или явно разрешённый project | Подтверждённые preferences/facts и итоги задач в PostgreSQL; пересмотр через 180 дней |
| Знания | tenant/project/ACL | Документы в S3, метаданные PostgreSQL, поисковый индекс Qdrant до удаления |
| Артефакты | tenant/thread/run | Отчёты, код, CSV в S3 на 30 дней; sandbox хранит временные копии |

Scope памяти соответствует [концепции LangGraph](https://docs.langchain.com/oss/python/concepts/memory). Сроки — настраиваемые проектные значения. InMemorySaver используется в unit-тестах, SQLite saver допустим для однопроцессного прототипа. Основной стенд использует AsyncPostgresSaver. Setup/migrations выполняет отдельная deployment-команда.

## State и context

State: messages с reducer add_messages; summary с source_message_ids/version; plan с task_id/dependencies/status/result_ref; evidence_refs; artifact_refs; selected_model; pending_action_ids; remaining_steps; last_error.

Immutable RuntimeContext: tenant_id, user_id, project_id, thread_id, run_id, roles, ACL version, policy version, trace_id, fencing_token, deadline, cancellation token. Он формируется из auth principal, передаётся через ToolRuntime и скрыт от схемы аргументов LLM. Command(update=...) меняет только разрешённые поля и добавляет ToolMessage с правильным tool_call_id; права и бюджеты через него не меняются.

Root usage ledger хранится транзакционно в PostgreSQL. Параллельные подагенты резервируют токены и вызовы в одной транзакционной квоте; локальные счётчики не предотвращают двойное расходование.

## Управление контекстом

Для вызова: available = context_limit − output_reserve − safety_margin − system_and_tools. Пример для достаточно большой модели: input budget 24 000, output reserve 4 000, margin 2 000 токенов. Для меньшего окна значения уменьшаются автоматически.

При 70% input budget старые turns суммаризируются: цель, ограничения, решения, числа с единицами и source_id. Последние четыре полных turns сохраняются, пока помещаются. Нельзя оставлять tool result без tool call и наоборот. Большой результат выносится в S3: в контексте остаются excerpt, hash и reference. Context editing сохраняет последние три полезных tool bodies. Затем summary и, в крайнем случае, trim целых старых групп.

Если текущий ввод не помещается даже после offload, возвращается INPUT_TOO_LARGE. Summary не служит первичным доказательством. RemoveMessage меняет активный state, но не удаляет старые checkpoints, traces и резервные копии. Для «забыть» нужен отдельный deletion workflow.

## Долгосрочная память

1. Явное «запомни» или итог задачи создаёт candidate с source_message_id и scope.
2. Валидатор исключает secrets и неподтверждённые выводы модели. Документ или web-страница не может записать предпочтение от имени пользователя.
3. Пользователь подтверждает candidate в панели памяти. Сохранение настройки самим пользователем уже является подтверждением.
4. MemoryService записывает normalized_key, value, provenance, confidence, valid_from, expires_at, version. Optimistic locking защищает от параллельных правок.
5. Старое значение становится superseded. Конфликт отображается пользователю; явная коррекция имеет приоритет.
6. Outbox обновляет отдельную Qdrant collection. При сбое индекса точное чтение PostgreSQL остаётся доступным.

Namespace: (tenant_id, user_id, preferences) или (tenant_id, project_id, project_facts). Application-owned MemoryStoreAdapter реализует Store над canonical records. Не создаём вторую независимую копию фактов в PostgresStore. Допустима замена на штатный PostgresStore, если он становится единственным canonical store.

Recall: серверный scope, active/unexpired records, top 5, максимум 1 000 токенов, повторная проверка canonical store. Пользователь видит использованную память, может исправить, отключить и удалить её. Пароли и API keys не хранятся как память.

## Ingestion

upload → quarantine → validate → parse → normalize → chunk → embed → staging upsert → publish version → ready.

Форматы: PDF с текстом, DOCX, PPTX, TXT/MD, CSV. Сканы требуют отдельной OCR-стадии с явным статусом. Парсер работает с лимитами, без макросов и внешних ссылок. На файл: 25 MiB, распакованное содержимое 200 MiB, максимум 1 000 страниц. Проверяются MIME по содержимому, архивные бомбы, число объектов и таймаут.

Сохраняются page/slide, headings, таблицы и их координаты. Стартовый chunking: 600 токенов, overlap 100, границы разделов, заголовки таблиц вместе со строками. Для CSV индексируются схема колонок и образцы, а точные вычисления выполняются по оригиналу в sandbox.

Метаданные chunk: document_id/version, ordinal, locator, text/hash, parser_revision, embedding_revision. Идемпотентность загрузки: tenant/project/content hash/parser version/embedding version. Между tenants не раскрывается совпадение файлов.

Версия становится ready после upsert всех точек. PostgreSQL хранит active_version. Retrieval повторно проверяет её, поэтому staged и устаревшие points не попадут в ответ. Outbox/reconciliation исправляют частичный upsert и удаляют старые точки после публикации новой версии.

## Qdrant и поиск

Collection `knowledge_<embedding_revision>`: named dense vector, cosine distance, размерность D выбранной embedding model; sparse vector для лексического поиска. Отдельная collection `memory_<embedding_revision>`. D проверяется при старте. Смена embedding model требует новой collection, переиндексации, проверки recall и переключения alias.

Payload knowledge: tenant_id, project_id, document_id, document_version, chunk_id, visibility, owner_user_id, acl_groups, text, locator, content_hash, embedding_revision, language. Индексируются поля tenant/project/document/visibility/owner/acl_groups. Для памяти отдельно user_id, status, expires_at.

Обязательный фильтр: tenant AND project AND (project-visible OR owner OR allowed-group). User memory: tenant AND user; project memory читается отдельным разрешённым запросом. Модель не получает возможность задавать tenant или отключать фильтры. Возможности [hybrid query](https://qdrant.tech/documentation/search/hybrid-queries/) и [multitenancy](https://qdrant.tech/documentation/manage-data/multitenancy/) подтверждены документацией Qdrant; правила доступа задаёт приложение.

Retrieval pipeline:

1. Нормализовать вопрос, раскрыть ссылки «тот документ» из thread.
2. Dense top 30 + sparse top 30 с одинаковыми ACL filters.
3. RRF fusion, дедупликация, до 40 кандидатов.
4. Проверить canonical ACL, active_version и tombstone **до** внешнего reranker/LLM.
5. Rerank top 6, максимум 6 000 токенов. Один bounded refill при удалении кандидатов post-filter.
6. Создать evidence: source_id, excerpt, locator, version, hash, fetched_at.
7. Сформировать ответ с источниками. При пробеле — явно сообщить о нём; web fallback только в разрешённом режиме.

Similarity score не является вероятностью истинности. Порог abstention калибруется на размеченных вопросах. При отказе reranker допустим результат fusion с диагностикой. Невозможность проверить ACL прекращает выдачу.

Противоречивые версии показываются с датами. Web не заменяет внутренний действующий регламент. Citation validator проверяет source_id, locator, доступ, версию и точность дословной цитаты; reviewer проверяет поддержку утверждения источником.

## Логическая модель данных

Все application tables имеют tenant_id. Composite FK (tenant_id, id) предотвращает межтенантные ссылки. RLS — дополнительная защита, application role без BYPASSRLS. Таблицы checkpoints принадлежат библиотеке, доступны только через gateway и opaque internal thread key.

| Таблица | Основные поля и ограничения |
|---|---|
| users/memberships | external_subject, tenant, role, project; unique membership |
| threads | owner, project, last_active_at, deletion_state |
| runs | thread, status, request_hash, idempotency_key, deadline, lease_token, policy_version |
| messages | thread, run, sequence, role, encrypted_content; unique thread/sequence |
| run_events | run, sequence, type, sanitized_payload; unique run/sequence |
| tasks | root_run, parent_task, role, status, result_ref |
| usage_ledger | run, task, attempt_id, kind, reserved, actual; unique attempt/kind |
| memory_records | scope, normalized_key, value, provenance, status, version, expires_at |
| documents | project, owner, visibility, active_version, deletion_state |
| document_versions | document, version, object_key, hash, parser_revision, state |
| chunks | document/version, ordinal, locator, text_ref, embedding_revision |
| sources | run, kind, document/object reference, locator, hash, fetched_at |
| artifacts | run, object_key, hash, MIME, size, scan_status, expires_at |
| approvals | run, action_hash, args, checkpoint_id, expiry, decision, decided_by |
| tool_executions | run, tool_call_id, args_hash, status, provider_request_id, result_ref |
| sandboxes | tenant/user/thread, provider_id, generation, state, lease, expiry |
| jobs/outbox | type, dedup_key, payload_ref, attempts, available_at, lease_until |
| deletion_jobs | scope, targets, cursor, state, requested_at, completed_at |
| audit_events | actor, action, object_id, outcome, policy_version, time |

Unique keys: runs tenant/user/idempotency_key, tool_executions run/tool_call_id. DDL и миграции создаются при реализации. SQL-tool не получает доступ к служебным таблицам.

## Удаление и восстановление

Удаление сразу ставит tombstone и запрещает retrieval. Job очищает Qdrant, originals, artifacts, messages, summaries, checkpoints и derived memory с удалённым provenance. Производные ответы с приватными excerpts тоже скрываются или удаляются. Удаление memory инвалидирует контекст threads, где она использовалась.

Пользователь получает завершение только после очистки online stores; цель — 24 часа. Backup retention 30 дней, после restore журнал удалений применяется до открытия трафика. Аудит хранит факт удаления без содержимого. Сбой Qdrant не раскрывает удалённый документ благодаря canonical post-filter.
