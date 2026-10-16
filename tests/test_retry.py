"""Unit tests for async retry utility."""

import pytest
from app.core.retry import retry_async


@pytest.mark.asyncio
async def test_retry_success_first_attempt() -> None:
    """Test function succeeding on the first attempt without retrying."""
    call_count = 0

    @retry_async(max_retries=3, base_delay=0.01)
    async def sample_fn() -> str:
        nonlocal call_count
        call_count += 1
        return "success"

    result = await sample_fn()
    assert result == "success"
    assert call_count == 1


@pytest.mark.asyncio
async def test_retry_success_after_failures() -> None:
    """Test function failing initially and succeeding within retry limit."""
    call_count = 0

    @retry_async(max_retries=3, base_delay=0.01, jitter=False)
    async def sample_fn() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError(f"Transient error #{call_count}")
        return "recovered"

    result = await sample_fn()
    assert result == "recovered"
    assert call_count == 3


@pytest.mark.asyncio
async def test_retry_exceeds_max_retries() -> None:
    """Test function exhausting all retries and re-raising the original exception."""
    call_count = 0

    @retry_async(max_retries=2, base_delay=0.01, jitter=False)
    async def sample_fn() -> None:
        nonlocal call_count
        call_count += 1
        raise ValueError("Persistent error")

    with pytest.raises(ValueError, match="Persistent error"):
        await sample_fn()

    # 1 initial attempt + 2 retries = 3 total calls
    assert call_count == 3


@pytest.mark.asyncio
async def test_retry_non_retryable_exception() -> None:
    """Test that exceptions not in retryable_exceptions are raised immediately."""
    call_count = 0

    @retry_async(
        max_retries=3,
        base_delay=0.01,
        retryable_exceptions=(ConnectionError,),
    )
    async def sample_fn() -> None:
        nonlocal call_count
        call_count += 1
        raise KeyError("Fatal lookup failure")

    with pytest.raises(KeyError, match="Fatal lookup failure"):
        await sample_fn()

    assert call_count == 1


@pytest.mark.asyncio
async def test_retry_on_retry_callback() -> None:
    """Test that on_retry callback is invoked on each failed attempt."""
    callbacks: list[tuple[int, float]] = []

    def handle_retry(exc: Exception, attempt: int, delay: float) -> None:
        callbacks.append((attempt, delay))

    @retry_async(
        max_retries=2,
        base_delay=0.05,
        backoff_factor=2.0,
        jitter=False,
        on_retry=handle_retry,
    )
    async def failing_fn() -> None:
        raise RuntimeError("Fail")

    with pytest.raises(RuntimeError):
        await failing_fn()

    assert len(callbacks) == 2
    assert callbacks[0][0] == 1
    assert callbacks[0][1] == pytest.approx(0.05, rel=1e-2)
    assert callbacks[1][0] == 2
    assert callbacks[1][1] == pytest.approx(0.10, rel=1e-2)


@pytest.mark.asyncio
async def test_retry_long_error_cut_to_200_chars(caplog: pytest.LogCaptureFixture) -> None:
    """Requirement 3: cut exception log message to at most 200 characters with '...' at the end and no details/body."""
    import logging

    long_message = "A" * 500
    details_dict = {"secret_payload": "hidden_data_12345", "response_body": "<html 500 error>"}

    class DetailedError(Exception):
        def __init__(self, msg: str, details: dict):
            super().__init__(msg)
            self.details = details

    @retry_async(max_retries=1, base_delay=0.01, jitter=False)
    async def sample_failing_fn() -> None:
        raise DetailedError(long_message, details_dict)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(DetailedError):
            await sample_failing_fn()

    # Verify log messages
    retry_records = [r for r in caplog.records if r.name == "app.core.retry"]
    assert len(retry_records) >= 1

    for record in retry_records:
        msg = record.getMessage()
        # No secret/details dict or body leaked
        assert "hidden_data_12345" not in msg
        assert "response_body" not in msg
        assert "details" not in msg.lower()
        # The logged exception string within the log is cut to at most 200 chars ending with ...
        # Check that the long 'A' string was truncated
        assert "A" * 201 not in msg
        assert "..." in msg

