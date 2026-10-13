"""Asynchronous rate limiter implementation using the token bucket algorithm."""

import asyncio
import time
from typing import Optional


class AsyncRateLimiter:
    """Token bucket rate limiter for controlling asynchronous API request throughput.

    Attributes:
        rate: Rate at which tokens are refilled per second.
        capacity: Maximum burst capacity of the token bucket.
    """

    def __init__(self, rate: float = 5.0, capacity: float = 10.0) -> None:
        """Initialize the rate limiter.

        Args:
            rate: Token refill rate per second (must be > 0).
            capacity: Maximum number of tokens that can accumulate (must be >= 1).
        """
        if rate <= 0:
            raise ValueError("Rate must be greater than 0")
        if capacity < 1:
            raise ValueError("Capacity must be at least 1")

        self.rate = float(rate)
        self.capacity = float(capacity)
        self.tokens = float(capacity)
        self.last_updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        """Internal helper to calculate and replenish tokens based on elapsed monotonic time."""
        now = time.monotonic()
        elapsed = now - self.last_updated
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_updated = now

    async def acquire(self, tokens: float = 1.0) -> None:
        """Acquire the specified number of tokens, sleeping if insufficient tokens exist.

        Args:
            tokens: Number of tokens requested (default is 1.0).
        """
        if tokens > self.capacity:
            raise ValueError(f"Requested tokens ({tokens}) exceeds total capacity ({self.capacity})")

        async with self._lock:
            while True:
                self._refill()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return

                # Calculate sleep duration required to replenish necessary tokens
                needed = tokens - self.tokens
                wait_time = needed / self.rate
                await asyncio.sleep(wait_time)

    async def __aenter__(self) -> "AsyncRateLimiter":
        """Async context manager entry that acquires a single token."""
        await self.acquire(1.0)
        return self

    async def __aexit__(self, exc_type: Optional[type], exc_val: Optional[BaseException], exc_tb: Optional[object]) -> None:
        """Async context manager exit."""
        return None
