"""Необязательный трекинг задач в Langfuse: без обоих ключей ничего не подключается.

На один run приходится один trace: trace_id выводится из run_id, поэтому продолжение после
подтверждения человеком и после перезапуска попадает в тот же trace. Вложенные вызовы модели
(reviewer, summary, специалисты) наследуют callback из контекста LangChain.

Граф передаёт в trace исходные сообщения, ещё не прошедшие DLP, поэтому mask применяет redact
ко всем данным. Сбой Langfuse не должен влиять на задачу: экспорт идёт в фоне, а ошибки
настройки только записываются в лог.
"""

import logging

from pydantic import BaseModel

from .security import redact

log = logging.getLogger(__name__)


def clean(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [clean(item) for item in value]
    if isinstance(value, BaseModel):
        return clean(value.model_dump())
    # Неизвестный объект нельзя отдавать сериализатору SDK: внутри него не будет маскирования.
    return redact(str(value))


def mask(*, data, **kwargs):
    return clean(data)


def handler_class():
    from langfuse.langchain import CallbackHandler

    class TypeOnlyErrorHandler(CallbackHandler):
        """Текст ошибки идёт мимо mask, а в теле ошибки провайдера может быть текст запроса."""

        def _get_error_level_and_status_message(self, error):
            level, _ = super()._get_error_level_and_status_message(error)
            return level, type(error).__name__

    return TypeOnlyErrorHandler


class Tracing:
    def __init__(self, settings):
        self.client = None
        self.public_key = settings.langfuse_public_key
        secret_key = settings.langfuse_secret_key.get_secret_value()
        if not (self.public_key and secret_key):
            if self.public_key or secret_key:
                log.warning("Langfuse отключён: нужны и LANGFUSE_PUBLIC_KEY, и LANGFUSE_SECRET_KEY")
            return
        try:
            from langfuse import Langfuse

            self.client = Langfuse(
                public_key=self.public_key,
                secret_key=secret_key,
                base_url=settings.langfuse_base_url,
                mask=mask,
            )
        except Exception as exc:
            log.warning("Langfuse отключён: %s", type(exc).__name__)

    def run_config(self, run_id, thread_id, owner):
        """Добавка к config графа для одного запуска; без Langfuse — пустая."""
        if not self.client:
            return {}
        try:
            from langfuse import Langfuse

            handler = handler_class()(
                public_key=self.public_key,
                trace_context={"trace_id": Langfuse.create_trace_id(seed=run_id)},
            )
        except Exception as exc:
            log.warning("Langfuse: trace не создан: %s", type(exc).__name__)
            return {}
        return {
            "callbacks": [handler],
            "metadata": {
                "langfuse_trace_name": "support-run",
                "langfuse_session_id": thread_id,
                "langfuse_user_id": owner,
                "run_id": run_id,
            },
        }

    def close(self):
        if not self.client:
            return
        try:
            self.client.shutdown()
        except Exception as exc:
            log.warning("Langfuse: не удалось отправить остаток данных: %s", type(exc).__name__)
