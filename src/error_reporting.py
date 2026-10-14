import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from urllib.parse import urlsplit, urlunsplit


class ErrorReporter(ABC):
    @abstractmethod
    def report(self, error: Exception, *, context: Mapping[str, object]) -> None:
        """Report an exception with its traceback and operation context."""
        pass


class ConsoleErrorReporter(ErrorReporter):
    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger if logger is not None else logging.getLogger('job_searcher.errors')

    def report(self, error: Exception, *, context: Mapping[str, object]) -> None:
        self._logger.error(
            'Error context: %s', json.dumps(dict(context), ensure_ascii=False, default=str),
            exc_info=(type(error), error, error.__traceback__),
        )


@dataclass
class _Scope:
    context: dict[str, object]
    reported_error: Exception | None = None


_scope: ContextVar[_Scope | None] = ContextVar('error_reporting_scope', default=None)


def report_error(reporter: ErrorReporter, error: Exception, **context: object) -> None:
    """Report a handled failure, including the active search context when present."""
    scope = _scope.get()
    details = {**(scope.context if scope else {}), **context}
    try:
        reporter.report(error, context=details)
    except Exception:
        # A reporting backend must not change search/cache failure behavior.
        logging.getLogger(__name__).exception('Error reporting failed')


@contextmanager
def report_errors(reporter: ErrorReporter, **context: object) -> Iterator[None]:
    """Report escaping failures once across nested scopes, then re-raise them.

    Context is isolated per async task and is carried into asyncio.to_thread.
    Worker updates remain visible to the awaiting task through the scope object.
    """
    parent = _scope.get()
    scope = _Scope({**(parent.context if parent else {}), **context})
    token = _scope.set(scope)
    try:
        yield
    except Exception as error:
        if scope.reported_error is not error:
            report_error(reporter, error)
        if parent is not None:
            parent.reported_error = error
        raise
    finally:
        _scope.reset(token)


def update_error_context(**context: object) -> None:
    """Update the active operation without coupling providers to a backend."""
    scope = _scope.get()
    if scope is not None:
        scope.context.update(context)


def report_async_errors[**P, T](reporter: ErrorReporter, **context: object) -> Callable[
    [Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]
]:
    """Apply a reporting scope to an async entry point."""
    def decorate(function: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            with report_errors(reporter, **context):
                return await function(*args, **kwargs)
        return wrapped
    return decorate


def posting_context(record: object) -> dict[str, str | int]:
    """Select posting identifiers, avoiding descriptions and arbitrary payloads."""
    if not isinstance(record, dict):
        return {}
    details = {}
    for key in ('id', 'objectID', 'reference', 'fingerprint', 'title', 'position', 'name',
                'slug', 'url', 'apply_url', 'permalink', 'ctaUrl', 'link'):
        value = record.get(key)
        if type(value) not in (str, int):
            continue
        if key in ('url', 'apply_url', 'permalink', 'ctaUrl', 'link'):
            if not isinstance(value, str):
                continue
            try:
                parts = urlsplit(value)
                # Tracking queries and embedded credentials are unnecessary for diagnosis.
                value = urlunsplit((parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path, '', ''))
            except ValueError:
                continue
        details[key] = value
    return details
