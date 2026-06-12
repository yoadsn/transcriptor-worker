"""Retry utility for storage I/O operations."""

import logging
import time
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_DEFAULT_DELAYS = (0.5, 1.0, 2.0)  # seconds between attempts


def with_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    delays: tuple[float, ...] = _DEFAULT_DELAYS,
    operation_label: str = "operation",
) -> T:
    """Call *fn* up to *attempts* times with exponential back-off on failure.

    Args:
        fn: Zero-argument callable to execute.
        attempts: Maximum number of attempts (including the first try).
        delays: Sequence of sleep durations (seconds) between consecutive
            attempts.  If fewer delay values are provided than retries needed,
            the last value is reused.
        operation_label: Human-readable label for log messages.

    Returns:
        The return value of *fn* on success.

    Raises:
        The last exception raised by *fn* if all attempts fail.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < attempts:
                delay_idx = min(attempt - 1, len(delays) - 1)
                delay = delays[delay_idx]
                logger.debug(
                    "%s failed (attempt %d/%d), retrying in %.1fs: %s",
                    operation_label,
                    attempt,
                    attempts,
                    delay,
                    exc,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "%s failed after %d attempts: %s",
                    operation_label,
                    attempts,
                    exc,
                )
    raise last_exc  # type: ignore[misc]
