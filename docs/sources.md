# Источники и совместимость

Основной источник: презентация `C:/documents/проект_УРФУ/agent_netology/agents (1).pptx`, 66 слайдов. Текст извлечён из XML PPTX в [slides-extracted.txt](slides-extracted.txt). Схемы 3, 24, 38, 46 прочитаны по встроенным изображениям. Исходный PPTX не изменялся.

Официальная документация проверена при проектировании 29 октября 2026 года:

| Источник | Что использовано |
|---|---|
| [LangChain middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in) | Категории lifecycle extensions: retries, fallback, summary и обработка tools |
| [Deep Agents overview](https://docs.langchain.com/oss/python/deepagents/overview) | Harness поверх LangChain/LangGraph, файлы, planning и subagents |
| [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) | Checkpoint, thread_id, resume и повторный вход в узел |
| [Memory concepts](https://docs.langchain.com/oss/python/concepts/memory) | Thread-scoped state и cross-thread memory |
| [Deep Agents sandboxes](https://docs.langchain.com/oss/python/deepagents/sandboxes) | Backend изолированного выполнения и файловые tools |
| [Qdrant hybrid queries](https://qdrant.tech/documentation/search/hybrid-queries/) | Dense/sparse retrieval и fusion |
| [Qdrant multitenancy](https://qdrant.tech/documentation/manage-data/multitenancy/) | Организация tenant scope в векторном хранилище |

Численные бюджеты, сроки, SLO, роли, schema/API и способ организации worker — проектные предложения, а не факты из этих источников. Проверка документации не заменяет запуск точной комбинации SDK.

## Что уточняется в compatibility spike

- Deep Agents и middleware API меняются. Проверить фактические импорты, async hooks, structured-output strategy, ToolRuntime, checkpoint setup, streaming и interrupt payload на зафиксированных версиях.
- Todo включать явно по выбранной версии; не полагаться на default из другого релиза.
- Не копировать model IDs из презентации без проверки доступности и capabilities. Model registry отделяет продукт от конкретного провайдера.
- Выбранный E2B adapter должен пройти network/resource/lifecycle contract tests. Для LangSmith, Daytona, Modal, Runloop и Vercel предусматривается тот же adapter boundary; интеграции не заявляются реализованными или проверенными.
- При замене провайдера проверить egress deny, TTL, kill, файловую передачу, регион данных, квоты и стоимость. При несовместимости адаптер не активируется.
- SearchProvider выбран через интерфейс; candidate Tavily требует отдельной проверки актуального API, условий и smoke-теста перед фиксацией реализации.
- Native PostgresStore может заменить собственный adapter только с сохранением единственного источника истины, namespace, provenance и deletion semantics.

## Интерпретация презентации

Примеры аренды, погоды, SQL и email объясняют классы инструментов. В выбранном продукте они представлены маршрутизацией, external read adapter, безопасным query registry и preview/commit интеграций. Pangoline/PVS на схеме 24 не приняты как обязательные продукты: функции истории и векторного поиска распределены между PostgreSQL и Qdrant согласно запросу.

Structured output обеспечивает форму, но не истинность. PII regex не обнаруживает все секреты. Summary и RemoveMessage не равны полному удалению. virtual_mode не изолирует shell. Эти уточнения включены в проект, чтобы учебные примеры не превратились в небезопасные production assumptions.
