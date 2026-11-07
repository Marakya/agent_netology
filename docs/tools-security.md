# Инструменты и безопасность

## Общий контракт

Tool определяется как типизированная функция с описанием и JSON Schema/Pydantic input, регистрируется через @tool. Обязательные metadata: name, version, required_role, data_classes, side_effect, approval_policy, timeout, max_input/output_bytes, retry_policy, cache_policy. Tenant, user, run и credentials поступают из ToolRuntime/server context, не от LLM.

Результат: status=ok/error/partial, data, source_ids, artifact_ids, error={code,message,retryable}|null, truncated, synthetic. Большие данные сохраняются как артефакт с hash. Ошибка для модели не содержит stack trace, токенов и внутреннего адреса БД; подробная очищенная диагностика доступна оператору по trace_id.

| Tool | Аргументы и результат | Политика |
|---|---|---|
| rag_search | query, document_ids?, top_k 1..6 → excerpts/source_ids | Только разрешённые документы, 10 s |
| read_source | source_id → excerpt и locator | Повторная проверка ACL, 5 s |
| web_search | query, max_results 1..5, freshness? → snippets/URLs | До 4 вызовов/run, 15 s; snippets ещё не доказательства |
| web_fetch | URL → очищенный текст/source_id | До 6 вызовов/run, 15 s, 2 MiB |
| run_python | code, input_artifact_ids → stdout/artifacts | Только sandbox, до 60 s |
| execute | argv либо shell command → exit_code/stdout/stderr | Только sandbox, до 60 s, без host shell |
| ls/read_file | path, offset/limit → список/строки | Только workspace sandbox, пагинация |
| write_file/edit_file | path, content либо old/new → hash | Workspace, quota, optimistic hash check |
| glob/grep | pattern, path, glob?, output_mode → совпадения | Workspace, ограничение результатов/времени |
| read_memory | query? → active facts | Tenant/user/project scope из context |
| propose_memory | key, value, source_message_id → candidate | Требует подтверждения до commit |
| query_dataset | dataset_id, query_name, typed params → rows | Только заранее разрешённые read-only views |
| integration_preview | connector, operation, typed fields → diff | Никакой внешней записи |
| integration_commit | proposal_id → external_record_id | editor + approver, idempotency, HITL |
| task | role, task, input_refs → child result | Глубина 1, общий budget |
| write_todos | tasks/status → plan version | Только текущий run |

Пример погоды из презентации — учебный образец внешнего read tool. При необходимости WeatherProvider реализуется тем же контрактом с единицами, временем наблюдения и source_id; отдельный погодный агент не требуется для выбранного кейса. Примеры CRM/Notion/email покрывает adapter интеграций. В учебном стенде используется внутренний демонстрационный реестр заявок, а не настоящая рассылка.

## Интернет

WebSearchProvider обращается к выбранному search API. Запрос проходит DLP: секреты и личные данные не отправляются; закрытые excerpts не пересылаются поисковику целиком. Приватные запросы блокируются или обезличиваются с уведомлением. API key хранится в сервисе, никогда в prompt и sandbox.

FetchGateway принимает только http/https, запрещает credentials в URL, нестандартные порты, localhost, private/link-local/reserved IPv4/IPv6, metadata endpoints и схемы file/data/ftp. DNS проверяется для всех A/AAAA, подключение привязывается к проверенному адресу с корректным Host/SNI. Каждый redirect (максимум 3) проходит ту же проверку. Egress firewall дублирует защиту от SSRF/DNS rebinding. Простого сравнения строк URL недостаточно.

Лимит 2 MiB применяется к потоку после распаковки, а также к compressed body; ограничены connect/read/total timeouts. JS, cookies, auth и выполнение загруженных файлов отключены. HTML очищается от scripts/styles, но текст всё равно считается недоверенным. robots/условия провайдера учитываются в настройках crawler. Browser automation не входит в минимальный fetch.

Источник хранит URL, title, extracted text/hash, fetched_at, published_at если известен. Неизвестная дата публикации не заменяется датой загрузки. Внешняя информация хранится в run evidence и не попадает в постоянную корпоративную KB без отдельного импорта. Если страница недоступна, snippet маркируется как ограниченное свидетельство.

## Sandbox

Sandbox — удалённая среда выполнения. Backend — интерфейс к её файлам и командам. Такое разделение описано в [документации sandboxes](https://docs.langchain.com/oss/python/deepagents/sandboxes). Ни FilesystemBackend, ни LocalShellBackend сами не изолируют процессы; virtual_mode ограничивает файловые пути, но не произвольный shell.

Выбран E2B. Adapter обязан реализовать create, upload, execute, download, inspect, terminate. До принятия провайдера проверяются реальные возможности network deny, ограничения ресурсов, TTL и kill. Если нужная политика не обеспечена выбранным тарифом/API, инструмент остаётся disabled; приложение не подменяет sandbox локальным subprocess.

Для create_agent sandbox используется через явные @tool wrappers. В Deep Agents передаётся sandbox backend с проверенными файловыми/execute tools. Авторизация, usage и обработка файлов действуют в обоих путях. Provider-specific constructors из слайдов проверяются при реализации.

Жизненный цикл:

1. Manager создаёт sandbox по tenant/user/thread + generation, максимум один активный writer.
2. Проверенные входы копируются из S3 в /input (read-only для задач). Рабочие файлы — /workspace, результаты — /output.
3. Worker выполняет команду, ограничивает stdout/stderr, время и объём файлов. Secrets приложения, SSH keys, host mounts и Docker socket отсутствуют.
4. Перед завершением результата Manager выгружает разрешённые /output files, проверяет MIME, размер, hash, сканирует и сохраняет в S3.
5. После подтверждения S3 запись artifact становится ready. Только затем можно выдать ссылку и завершить sandbox.
6. Cleanup в finally дополняется janitor: он закрывает orphaned sandboxes после сбоя процесса. При cancel команда останавливается, среда уничтожается при необходимости.

Начальные лимиты: 2 vCPU, 2 GiB RAM, 1 GiB disk, 60 s/command, 10 min idle TTL, 30 min absolute lifetime, 100 KiB stdout/stderr, 20 MiB/artifact, 50 MiB output/run. Лимиты являются требованиями к adapter, а не обещанием конкретного провайдера.

Egress выключен, зависимости заранее включены в immutable image. Открытый pip install и web внутри sandbox не нужны: интернет доступен отдельными tools. При ожидании approval файлы выгружаются, sandbox освобождается; resume создаёт новую generation и восстанавливает workspace manifest из S3. Checkpoint хранит ссылки, не live sockets/process handles.

Пути нормализуются, запрещены traversal, NUL, ссылки за пределы workspace и подмена symlink между проверкой и чтением. Download не следует symlinks. HTML/JS artifacts отдаются attachment или с отдельного origin без доступа к cookies API.

## Файловые tools

ls возвращает абсолютные виртуальные пути. read_file поддерживает offset с 0 и limit (default 100), номера строк с 1, пустой файл как корректный пустой результат. Длинные строки и общий output ограничены, выдаётся truncated=true и reference для дочитывания.

write_file по умолчанию создаёт новый файл; overwrite требует ожидаемый hash. edit_file требует предварительного чтения и expected_hash, old_string должен быть уникален при replace_all=false. Номера строк не входят в old/new text. Это проверяется кодом, а не только описанием tool.

glob поддерживает *, **, ?, {a,b} в пределах workspace. grep в публичном контракте ищет буквальную строку, как на слайде 44; output_mode=files_with_matches/content/count. Regex не включается неявно. Результат ограничен 200 совпадениями и timeout, при усечении сообщается об этом.

## SQL и интеграции

query_dataset выбирает заранее зарегистрированный параметризованный query_name. Для расширенного SQL-профиля нужны AST validation, read-only transaction, отдельный DB user, доступ только к analytics views, statement timeout 3 s, max 1 000 rows; один startswith SELECT не защищает от функций с побочным эффектом. На учебном этапе предпочтителен registry запросов. Производственная БД приложения агенту не доступна.

integration_commit принимает сохранённый proposal_id, а не свободный URL и credentials. Approval привязан к нормализованным args, версии записи, action_hash и сроку. Изменение аргументов пересчитывает hash и проходит те же проверки. Успешный commit записывает provider_request_id. Неопределённый результат после timeout требует reconciliation, а не слепого retry.

## Threat model

| Угроза | Защита и проверка |
|---|---|
| Prompt injection в PDF/web | Недоверенные данные, tool policy вне LLM; попытка отправить secrets не выполняется |
| Чужой thread/document/artifact | Auth + ownership + ACL + composite keys/RLS; отрицательные cross-tenant tests |
| Memory poisoning | Provenance, подтверждение, запрет self-update системных инструкций |
| SSRF и DNS rebinding | IP-aware fetch, redirect revalidation, network policy |
| Выполнение на хосте | Только sandbox provider, fail closed при недоступности |
| Escape через files/symlinks | Workspace boundaries и безопасный export |
| Бесконечный loop | Общие call/token/time limits включая children/retries |
| Повтор внешнего действия | Durable action ledger и provider idempotency/reconciliation |
| PII в traces | Redact/mask до внешнего telemetry sink, запрет raw args |
| Вредоносный upload | Quarantine, parser limits, сканирование, без макросов |

PII detection использует built-in detectors и проверенные custom detectors, включая телефон; regex не гарантирует обнаружение всех персональных данных. Политика данных действует до отправки модели, поиску и reranker. HITL не заменяет права и sandbox, а sandbox не устраняет prompt injection.
