# OpenAI + Qdrant: настройка

Агент всегда работает с реальной LLM и embeddings. Устройство описано в [spec.md](../spec.md), здесь — только настройка провайдеров.

## Локально

```powershell
Copy-Item .env.example .env   # если .env ещё нет
```

Минимум в `.env`:

```dotenv
OPENAI_API_KEY=ваш_ключ
MODEL_FAST=gpt-4.1-mini
MODEL_REASONING=gpt-4.1
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
QDRANT_URL=
```

Пустой `QDRANT_URL` включает встроенный Qdrant с сохранением в `DATA_DIR/qdrant`. Запуск: `run-streamlit.ps1` (http://127.0.0.1:8501) или `run.ps1` (http://127.0.0.1:8000).

Платный API вызывается уже при загрузке документа (embeddings), а затем на каждом шаге агента. При ошибке ключа или квоты задача завершается статусом `failed`, тип ошибки виден в журнале действий. Тихого перехода на заглушку нет.

`OPENAI_BASE_URL` можно направить на совместимый провайдер, если он поддерживает Chat Completions с tools и embeddings с параметром `dimensions`.

## Отдельный сервер Qdrant

```powershell
docker compose -f compose.qdrant.yaml up -d
```

В `.env` задайте `QDRANT_URL=http://127.0.0.1:6333` и отдельный `DATA_DIR`, перезапустите приложение и загрузите документы заново. Для Qdrant Cloud задайте URL и `QDRANT_API_KEY`. Остановка — `docker compose -f compose.qdrant.yaml down` (без `-v`, чтобы сохранить индекс).

Приложение и Qdrant вместе в Docker: `docker compose up --build` с `OPENAI_API_KEY` и `APP_TOKEN` в `.env`. Compose сам задаёт внутренний URL Qdrant.

Смена backend Qdrant, embedding-модели или размерности требует повторной загрузки документов: имя коллекции зависит от модели и размерности. Даже с серверным Qdrant запускайте один процесс API.

## Диагностика

`GET /api/config` возвращает модель, embedding-модель и размерность, имя коллекции и инструменты, которые сейчас предлагаются модели. Ключи в ответ не попадают. Это конфигурация, а не проверка связи с провайдером.

Протокольный тест `uv run pytest tests/test_openai_rag.py -q` использует настоящий OpenAI SDK и настоящий Qdrant с подменой HTTP-ответов. Ваш аккаунт он не проверяет.

Документация API: [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling), [OpenAI embeddings](https://developers.openai.com/api/docs/guides/embeddings).
