"""Asynchronous retry utility with exponential backoff and jitter."""

import asyncio
from functools import wraps
import logging
import random
from typing import Any, Callable, Coroutine, Optional, Sequence, Tuple, Type, TypeVar, Union

logger = logging.getLogger(__name__)

T = TypeVar("T")


def retry_async(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: Union[Type[Exception], Tuple[Type[Exception], ...]] = (Exception,),
    on_retry: Optional[Callable[[Exception, int, float], None]] = None,
) -> Callable[[Callable[..., Coroutine[Any, Any, T]]], Callable[..., Coroutine[Any, Any, T]]]:
    """Decorator for retrying async coroutines with exponential backoff and jitter.

    Args:
        max_retries: Maximum number of retry attempts before re-raising the exception.
        base_delay: Initial delay in seconds before the first retry.
        max_delay: Maximum delay cap in seconds.
        backoff_factor: Multiplier for exponential backoff between attempts.
        jitter: Whether to add random jitter to delay to prevent synchronized retries.
        retryable_exceptions: Exception type or tuple of exceptions that should trigger retries.
        on_retry: Optional callback invoked before sleeping, passed (exception, attempt, delay).

    Returns:
        Decorated async function that automatically retries on specified exceptions.
    """
    if isinstance(retryable_exceptions, type) and issubclass(retryable_exceptions, Exception):
        exceptions_tuple: Tuple[Type[Exception], ...] = (retryable_exceptions,)
    else:
        exceptions_tuple = tuple(retryable_exceptions)

    def decorator(
        func: Callable[..., Coroutine[Any, Any, T]]
    ) -> Callable[..., Coroutine[Any, Any, T]]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            attempt = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except exceptions_tuple as exc:
                    attempt += 1
                    if attempt > max_retries:
                        logger.error(
                            "Function %s failed after %d retries. Raising: %s",
                            func.__name__,
                            max_retries,
                            exc,
                        )
                        raise

                    # Exponential backoff formula: base_delay * (backoff_factor ** (attempt - 1))
                    raw_delay = base_delay * (backoff_factor ** (attempt - 1))
                    delay = min(max_delay, raw_delay)
                    if jitter:
                        # Full jitter uniformly distributed between 0.5 * delay and 1.5 * delay
                        delay = delay * random.uniform(0.5, 1.5)

                    logger.warning(
                        "Attempt %d/%d for %s failed with %s: %s. Retrying in %.2fs",
                        attempt,
                        max_retries,
                        func.__name__,
                        type(exc).__name__,
                        exc,
                        delay,
                    )

                    if on_retry:
                        try:
                            on_retry(exc, attempt, delay)
                        except Exception as cb_err:
                            logger.debug("on_retry callback error: %s", cb_err)

                    await asyncio.sleep(delay)

        return wrapper

    return decorator
