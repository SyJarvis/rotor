import asyncio
import logging
from typing import Callable, TypeVar, Optional, List
from httpx import HTTPStatusError, TimeoutException, ConnectError

from rotor.config import settings
from rotor.core.exceptions import ChannelException

logger = logging.getLogger(__name__)

T = TypeVar('T')


class RetryConfig:
    """Configuration for retry logic."""

    def __init__(
        self,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        max_delay: float = 30.0,
        backoff_multiplier: float = 2.0,
        jitter: bool = True
    ):
        """
        Initialize retry configuration.

        Args:
            max_retries: Maximum number of retry attempts
            initial_delay: Initial delay in seconds
            max_delay: Maximum delay between retries
            backoff_multiplier: Multiplier for exponential backoff
            jitter: Whether to add random jitter to delays
        """
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.backoff_multiplier = backoff_multiplier
        self.jitter = jitter


class RetryStrategy:
    """Base retry strategy."""

    def __init__(self, config: RetryConfig):
        self.config = config

    def should_retry(self, attempt: int, error: Exception) -> bool:
        """
        Determine if a request should be retried.

        Args:
            attempt: Current attempt number
            error: The error that occurred

        Returns:
            True if should retry, False otherwise
        """
        if attempt >= self.config.max_retries:
            return False
        return self.is_retryable_error(error)

    def is_retryable_error(self, error: Exception) -> bool:
        """Check if an error is retryable."""
        # Default: network errors and 5xx errors are retryable
        if isinstance(error, (TimeoutException, ConnectError)):
            return True
        if isinstance(error, HTTPStatusError):
            status = error.response.status_code
            return 500 <= status < 600 or status == 429
        return False

    def get_delay(self, attempt: int) -> float:
        """
        Calculate delay before next retry.

        Args:
            attempt: Current attempt number (0-indexed)

        Returns:
            Delay in seconds
        """
        delay = min(
            self.config.initial_delay * (self.config.backoff_multiplier ** attempt),
            self.config.max_delay
        )

        if self.config.jitter:
            import random
            delay = delay * (0.5 + random.random() * 0.5)

        return delay


class RetryService:
    """
    Service for handling retries with exponential backoff.
    """

    def __init__(self, config: Optional[RetryConfig] = None):
        """
        Initialize the retry service.

        Args:
            config: Retry configuration (uses default if not provided)
        """
        self.config = config or RetryConfig(
            max_retries=settings.MAX_RETRIES,
            initial_delay=settings.RETRY_DELAY
        )
        self.strategy = RetryStrategy(self.config)

    async def execute_with_retry(
        self,
        func: Callable[..., Callable[[], T]],
        *args,
        **kwargs
    ) -> T:
        """
        Execute a function with retry logic.

        Args:
            func: The function to execute (may be async)
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            The result of the function call

        Raises:
            ChannelException: If all retries are exhausted
        """
        last_error = None

        for attempt in range(self.config.max_retries + 1):
            try:
                if asyncio.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)

                # Log success if it was a retry
                if attempt > 0:
                    logger.info(f"Request succeeded on attempt {attempt + 1}")

                return result

            except Exception as e:
                last_error = e

                if not self.strategy.should_retry(attempt, e):
                    logger.warning(
                        f"Request failed with non-retryable error: {type(e).__name__}: {e}"
                    )
                    raise

                # Calculate delay
                delay = self.strategy.get_delay(attempt)
                logger.warning(
                    f"Request failed on attempt {attempt + 1}, "
                    f"retrying in {delay:.2f}s... Error: {type(e).__name__}: {e}"
                )

                await asyncio.sleep(delay)

        # All retries exhausted
        raise ChannelException(
            f"Request failed after {self.config.max_retries} retries",
            original_error=str(last_error)
        )


async def retry_on_failure(
    func: Callable,
    max_retries: int = 3,
    delay: float = 1.0
):
    """
    Simple retry decorator/wrapper.

    Args:
        func: Function to retry
        max_retries: Maximum number of retries
        delay: Delay between retries in seconds
    """
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            if asyncio.iscoroutinefunction(func):
                return await func()
            else:
                return func()
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                await asyncio.sleep(delay)
                continue
            raise

    raise last_error
