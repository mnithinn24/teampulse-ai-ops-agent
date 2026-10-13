"""Unit tests for AsyncRateLimiter."""

import asyncio
import time
import pytest
from app.core.rate_limiter import AsyncRateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_initialization() -> None:
    """Test initialization parameter constraints."""
    with pytest.raises(ValueError, match="Rate must be greater than 0"):
        AsyncRateLimiter(rate=0)

    with pytest.raises(ValueError, match="Capacity must be at least 1"):
        AsyncRateLimiter(rate=1.0, capacity=0)


@pytest.mark.asyncio
async def test_rate_limiter_acquire_within_capacity() -> None:
    """Test that tokens can be acquired immediately when bucket has capacity."""
    limiter = AsyncRateLimiter(rate=10.0, capacity=5.0)
    start = time.monotonic()
    await limiter.acquire(1.0)
    await limiter.acquire(1.0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_rate_limiter_context_manager() -> None:
    """Test async context manager protocol."""
    limiter = AsyncRateLimiter(rate=5.0, capacity=2.0)
    async with limiter:
        pass


@pytest.mark.asyncio
async def test_rate_limiter_exceeds_capacity() -> None:
    """Test acquiring more tokens than total capacity raises ValueError."""
    limiter = AsyncRateLimiter(rate=5.0, capacity=2.0)
    with pytest.raises(ValueError, match="exceeds total capacity"):
        await limiter.acquire(3.0)


@pytest.mark.asyncio
async def test_rate_limiter_throttles() -> None:
    """Test that rate limiter throttles once initial capacity is depleted."""
    limiter = AsyncRateLimiter(rate=10.0, capacity=1.0)
    # Drain capacity
    await limiter.acquire(1.0)

    start = time.monotonic()
    # Next token should require waiting ~0.1s
    await limiter.acquire(1.0)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.08
