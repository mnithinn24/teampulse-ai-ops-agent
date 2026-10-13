"""Asynchronous retry utility with exponential backoff and jitter."""

import asyncio
from functools import wraps
import logging
import random
from typing import Any, Callable, Coroutine, Optional, Sequence, Tuple, Type, TypeVar, Union

logger = logging.getLogger(__name__)

T = TypeVar("T")


def is_retryable_llm_error(exc: Exception) -> bool:
    """Determine if an LLM error is retryable (429 or 503).

    Specifically:
    - Retries: 429 (Rate limit / Too Many Requests), 503 (Unavailable / Service Unavailable)
    - Never retries: 400, 401, 403, 404 (fails immediately)
    """
    # 1. Check explicit status_code or code attribute (numeric)
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code is not None:
        try:
            code_int = int(status_code)
            if code_int in (400, 401, 403, 404):
                return False
            if code_int in (429, 503):
                return True
        except (ValueError, TypeError):
            pass

    # 2. Check httpx response status if present
    if hasattr(exc, "response") and getattr(exc.response, "status_code", None) is not None:
        code_int = exc.response.status_code
        if code_int in (400, 401, 403, 404):
            return False
        if code_int in (429, 503):
            return True

    # 3. Check AnalysisError or IntegrationError details dict
    if hasattr(exc, "details") and isinstance(exc.details, dict):
        code_in_details = exc.details.get("status_code") or exc.details.get("code")
        if code_in_details is not None:
            try:
                code_int = int(code_in_details)
                if code_int in (400, 401, 403, 404):
                    return False
                if code_int in (429, 503):
                    return True
            except (ValueError, TypeError):
                pass

    # 4. Check chained __cause__ or __context__
    if exc.__cause__ is not None and exc.__cause__ is not exc:
        return is_retryable_llm_error(exc.__cause__)

    err_str = str(exc)
    # Check non-retryable keywords in error string
    non_retry_patterns = (
        "400",
        "401",
        "403",
        "404",
        "NOT_FOUND",
        "PERMISSION_DENIED",
        "BAD_REQUEST",
        "UNAUTHENTICATED",
    )
    if any(pat in err_str for pat in non_retry_patterns):
        return False

    # Check retryable keywords in error string
    retry_patterns = (
        "429",
        "503",
        "UNAVAILABLE",
        "RESOURCE_EXHAUSTED",
        "Too Many Requests",
        "Service Unavailable",
    )
    if any(pat in err_str for pat in retry_patterns):
        return True

    return False


def retry_async(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: Union[Type[Exception], Tuple[Type[Exception], ...]] = (Exception,),
    retry_if: Optional[Callable[[Exception], bool]] = None,
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
        retry_if: Optional predicate returning False if exception should NOT be retried.
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
                    if retry_if is not None and not retry_if(exc):
                        raise

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
                        # Full jitter uniformly distributed between 0.8 * delay and 1.2 * delay
                        delay = delay * random.uniform(0.8, 1.2)

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
