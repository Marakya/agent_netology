# API, интерфейс и эксплуатация

## HTTP и события

API /v1 использует bearer token от OIDC. Для локального запуска допустим явно включённый dev principal только на loopback. В public-режиме отсутствие auth приводит к отказу запуска. Tenant/user/roles не принимаются из JSON тела как источник полномочий.

| Метод и путь | Контракт |
|---|---|
| POST /threads | project_id, title? → 201 thread_id после проверки membership |
| GET /threads/{id}/messages | Cursor pagination, только владелец/разрешённый участник |
| POST /threads/{id}/runs | message, input_artifact_ids, mode=auto/simple/complex → 202 run_id |
| GET /runs/{id} | status, plan, usage, result?, pending_approvals |
| GET /runs/{id}/events | SSE с Last-Event-ID и сохранёнными sequence |
| POST /runs/{id}/cancel | Идемпотентный запрос отмены → 202/200 |
| POST /approvals/{id}/decision | decision, action_hash, expected_version, edited_args? |
| POST /documents | multipart + project_id → 202 document_id/job_id |
| GET /documents/{id} | ingestion state, version, error? |
| DELETE /documents/{id} | 202 deletion_job_id, immediate retrieval tombstone |
| GET /memory | Active/pending records текущего scope |
| PATCH /memory/{id} | value, expected_version, confirmation → новая версия |
| DELETE /memory/{id} | 202 deletion_job_id |
| DELETE /threads/{id} | Отмена run и полное удаление thread через job |
| GET /deletions/{id} | online purge status и сроки backup expiration |
| GET /artifacts/{id}/download | ACL, scan_status → короткоживущий download URL |
| GET /health/live, /health/ready | Процесс; готовность обязательных зависимостей |

Создающие операции требуют Idempotency-Key. Unique tenant/user/key + request hash: тот же запрос возвращает исходный result, другое тело с тем же ключом — 409. Авторизация повторяется при replay. Ключ и request hash хранятся не меньше срока жизни run. Ошибки: 400 schema, 401 auth, 403 permission, 404 чужой/неизвестный объект без раскрытия существования, 409 conflict, 413 size, 429 quota, 503 dependency.

Пример начала:

```json
{
  "message": "Рассчитай задержки по заказам и сопоставь с регламентом",
  "input_artifact_ids": ["art_orders"],
  "mode": "auto"
}
```

202: {run_id, thread_id, status:queued, events_url}. GET run возвращает тот же run после рестарта, а не создаёт новый.

SSE types: run.started, plan.updated, task.started, tool.started, tool.completed, approval.required, artifact.ready, answer.delta, usage.updated, run.completed, run.failed, run.cancelled. Envelope: event_id, run_id, sequence, occurred_at, type, payload. Доставка at-least-once, клиент дедуплицирует по sequence. Переподключение не перезапускает анализ. answer.delta — предварительный текст; окончательный ответ получает отдельное committed событие после validation. Tool stdout и PII не транслируются без фильтрации.

## Human-in-the-loop

UI показывает точное действие, аргументы/diff, внешнюю систему, ожидаемый эффект и срок. Доступны approve/edit/reject. Для опасных actions набор может быть только approve/reject. Подтверждение принадлежит аутентифицированному approver проекта, не произвольному обладателю thread_id.

```json
{
  "contract": "approval_decision",
  "decision": "edit",
  "action_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "expected_version": 1,
  "edited_args": {"title": "Проверить задержки", "priority": "normal"}
}
```

API проверяет статус pending, TTL, actor permission, version и hash. Edited args проходят schema/policy повторно и сохраняются как новая одобренная версия при наличии полномочий approver. Если edit меняет тип/систему действия или требует более высоких прав, создаётся новое approval, без выполнения. Решение durable сохраняется до enqueue resume.

LangGraph продолжает тот же thread/checkpoint через Command(resume=...). При нескольких interrupts решения сопоставляются по interrupt/action ID, не только позиции массива. Семантика [interrupt/resume](https://docs.langchain.com/oss/python/langgraph/interrupts) допускает повторный вход в узел; все действия до interrupt должны быть чистыми или идемпотентными.

Approval TTL 24 h. Повторный identical decision возвращает прежний результат, несовместимый — 409. Истёкшее согласие не используется. На resume проверяются актуальные ACL, объект version и policy. Долгий простой не удерживает worker и sandbox.

## Надёжность и бюджет

Root limits: 24 model attempts, 40 tool attempts, 48 000 суммарных input/output tokens, 300 s активного выполнения, 2 retries, recursion limit 60. Web search ≤4, fetch ≤6, sandbox commands ≤5. Ожидание человека не расходует active execution deadline, но имеет отдельный TTL. Все model calls, включая selector, summary, emulator, review и fallback, входят в ledger.

Дополнительные скользящие лимиты на 24 часа: 200 model attempts и 400 tool attempts на thread, 500 000 токенов на user. Они не сбрасываются созданием нового run; user quota не обходится созданием нового thread. Проверка и резерв выполняются атомарно вместе с root budget. Тарифные и организационные квоты могут только уменьшать разрешённый остаток.

До вызова атомарно резервируются максимальный output и оценка input. После ответа резерв заменяется фактическим usage. При timeout и неизвестном usage резерв сохраняется до reconciliation. Стоимость = сумма usage × датированные tariffs + sandbox time + search/embedding/rerank. Если тариф неизвестен, стоимость помечается unknown, а token/call limits продолжают работать. Денежный лимит включается только после заполнения тарифов.

Worker crash: lease истекает, другой worker продолжает checkpoint с новым fencing token. Работа может повториться, поэтому внешние эффекты защищены action ledger. Для read tools retry безопасен в пределах бюджета. Для write tool после неизвестного результата выполняется lookup provider_request_id/idempotency key. Если провайдер не поддерживает reconciliation, состояние becomes needs_review и автоматического повторения нет. Exactly-once для произвольного внешнего API не обещается.

Cancel: durable cancellation flag, запрет новых calls, отмена доступных in-flight requests, остановка sandbox, release leases. Уже подтверждённое внешнее действие не объявляется отменённым без проверки. Клиенту показываются реально завершённые эффекты.

| Отказ | Поведение |
|---|---|
| PostgreSQL | Fail closed: без доступа/checkpoints нельзя выполнять tools |
| Qdrant | Сообщить о недоступности KB, не придумывать внутренние факты; web только если уместен |
| Search/fetch | Дать ответ по доступным источникам с ограничением свежести |
| Sandbox | Расчёт unavailable, не переходить на host execution |
| Reranker | Разрешён fusion-only fallback после ACL check |
| LLM | Ограниченный retry, совместимый fallback, затем partial/failed |
| S3 | Не объявлять artifact ready, сохранять ссылку на pending export и повторять в пределах TTL |

## Интерфейс

Чат показывает источники рядом с выводами, раскрываемый plan, текущий этап и кнопку отмены. Documents показывает uploading/parsing/embedding/ready/failed с понятной причиной. Memory показывает pending/active facts, provenance, edit/delete и переключатель использования. Approval показывает конкретный diff. Artifacts показывает имя, MIME, размер и скачивание. История позволяет вернуться к задаче после рестарта.

Simulation mode имеет постоянную видимую метку «Синтетические результаты». Недоступный инструмент помечается unavailable; UI не выдаёт fake результат за реальный. При partial пользователь видит выполненную часть и оставшиеся ограничения.

## Развёртывание

Учебный профиль: UI, API, graph worker, ingestion worker, PostgreSQL, Qdrant, S3-compatible storage через Docker Compose; sandbox и search — внешние сервисы. Локальная машина не исполняет код LLM. Для полностью offline tests используются fakes, а не имитация безопасного sandbox.

Производственный профиль: TLS ingress, OIDC, managed PostgreSQL с PITR, Qdrant snapshots/replication по требованиям нагрузки, S3 versioning/encryption, secret manager, отдельные сети, egress proxy и worker autoscaling. API не имеет доступа к provider admin keys sandbox; выдаётся отдельная минимальная service identity.

Порядок старта: validate config → DB migrations/checkpointer setup → Qdrant collections/indexes → S3 bucket policy → workers → API readiness → UI. Миграции выполняет deployment job с отдельными правами, runtime не имеет DDL permissions.

Configuration: DATABASE_URL, QDRANT_URL/API_KEY, S3 endpoint/bucket/credentials, OIDC issuer/audience, MODEL_FAST/MODEL_REASONING, EMBEDDING_MODEL/REVISION/DIMENSION, SEARCH_PROVIDER/API_KEY, SANDBOX_PROVIDER/API_KEY, POLICY_PATH, OBSERVABILITY settings. Секреты не коммитятся, публичные bind-порты БД не открываются. Образы и Python dependencies фиксируются digest/lock.

Backup: PostgreSQL PITR, ежедневный Qdrant snapshot, S3 durable versioning. Проектные цели RPO 15 min, RTO 4 h проверяются restore drill; Qdrant можно перестроить по originals/chunks. Удаления повторно применяются до открытия доступа. Эти цели не измерены в текущем репозитории.

## Наблюдаемость

Каждый run связывает trace_id, thread_id, task_id, tool_call_id, provider request ID, prompt/model/policy versions. Метрики: latency p50/p95, queue depth, error/retry/fallback rates, tokens/cost, retrieval empty rate, citations coverage, approval wait, sandbox active/orphan count, ingestion/deletion lag.

Логи по умолчанию содержат IDs, коды и агрегаты без raw prompts/PII. Отладочный контент включается отдельно с ограниченным сроком и доступом. Alerts: orphan sandbox > TTL, queue age > 2 min, failed runs > 5% за 10 min, deletion lag > 24 h, budget exhaustion spike. Пороговые значения стартовые.

Цели стенда: 10 одновременных пользователей, до 10 000 документов; p95 ответа без sandbox <30 s, обычного сложного анализа <180 s. Измерять на фиксированных fixtures и моделях. Это критерии будущей приёмки, не заявленная производительность.
