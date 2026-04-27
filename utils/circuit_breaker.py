"""
Circuit Breaker for LLM and external service calls.

States:
  CLOSED    — normal operation, calls pass through
  OPEN      — failure threshold exceeded, calls rejected immediately
  HALF_OPEN — cooldown elapsed, one probe call allowed to test recovery

Usage:
    breaker = CircuitBreaker("ollama", failure_threshold=3, recovery_timeout=60.0)
    result = breaker.call(llm.invoke, prompt, fallback=lambda: None)
"""
import threading
import time
from enum import Enum
from typing import Any, Callable, Optional

from utils.logger import get_logger
from utils.metrics import record_event

logger = get_logger("circuit_breaker")


class State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpen(Exception):
    """Raised when a call is rejected because the circuit is open."""


class CircuitBreaker:
    """
    Thread-safe circuit breaker.

    failure_threshold  — consecutive failures before opening the circuit
    recovery_timeout   — seconds to wait in OPEN state before probing
    success_threshold  — consecutive successes in HALF_OPEN to re-close
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
        success_threshold: int = 2,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold

        self._state = State.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────

    @property
    def state(self) -> State:
        with self._lock:
            return self._evaluate_state()

    def call(
        self,
        func: Callable,
        *args,
        fallback: Any = None,
        **kwargs,
    ) -> Any:
        """
        Execute func(*args, **kwargs) through the breaker.

        If the circuit is OPEN, fallback is returned (callable fallback is invoked).
        If func raises, the failure is recorded; if threshold is hit the circuit opens.
        """
        with self._lock:
            current_state = self._evaluate_state()

        if current_state == State.OPEN:
            logger.warning(f"Circuit [{self.name}] OPEN — call rejected, returning fallback")
            record_event("circuit_breaker_rejected", {"name": self.name})
            return fallback() if callable(fallback) else fallback

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure(exc)
            logger.warning(f"Circuit [{self.name}] call failed: {exc}")
            record_event("circuit_breaker_failure", {
                "name": self.name,
                "error": str(exc)[:120],
                "failure_count": self._failure_count,
            })
            return fallback() if callable(fallback) else fallback

    def is_available(self) -> bool:
        return self.state != State.OPEN

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "state": self._state.value,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                "last_failure_time": self._last_failure_time,
            }

    def reset(self):
        """Manually reset the breaker to CLOSED (for testing/admin)."""
        with self._lock:
            self._state = State.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure_time = None
        logger.info(f"Circuit [{self.name}] manually reset to CLOSED")

    # ── Internal ──────────────────────────────────────

    def _evaluate_state(self) -> State:
        """Must be called with self._lock held."""
        if self._state == State.OPEN and self._last_failure_time is not None:
            elapsed = time.time() - self._last_failure_time
            if elapsed >= self.recovery_timeout:
                self._state = State.HALF_OPEN
                self._success_count = 0
                logger.info(f"Circuit [{self.name}] OPEN → HALF_OPEN (testing recovery)")
                record_event("circuit_breaker_state", {"name": self.name, "state": "half_open"})
        return self._state

    def _on_success(self):
        with self._lock:
            if self._state == State.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state = State.CLOSED
                    self._failure_count = 0
                    logger.info(f"Circuit [{self.name}] HALF_OPEN → CLOSED (recovered)")
                    record_event("circuit_breaker_state", {"name": self.name, "state": "closed"})
            elif self._state == State.CLOSED:
                self._failure_count = 0

    def _on_failure(self, exc: Exception):
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._failure_count >= self.failure_threshold:
                if self._state != State.OPEN:
                    self._state = State.OPEN
                    logger.error(
                        f"Circuit [{self.name}] → OPEN "
                        f"(failures={self._failure_count}, threshold={self.failure_threshold})"
                    )
                    record_event("circuit_breaker_state", {
                        "name": self.name,
                        "state": "open",
                        "trigger_error": str(exc)[:120],
                    })


# ── Module-level singleton breakers ──────────────────

_ollama_breaker: Optional[CircuitBreaker] = None
_mongo_breaker: Optional[CircuitBreaker] = None
_breaker_lock = threading.Lock()


def get_llm_breaker() -> CircuitBreaker:
    global _ollama_breaker
    if _ollama_breaker is None:
        with _breaker_lock:
            if _ollama_breaker is None:
                import os
                _ollama_breaker = CircuitBreaker(
                    name="ollama",
                    failure_threshold=int(os.getenv("CB_LLM_FAILURE_THRESHOLD", "3")),
                    recovery_timeout=float(os.getenv("CB_LLM_RECOVERY_TIMEOUT", "60.0")),
                    success_threshold=2,
                )
    return _ollama_breaker


def get_mongo_breaker() -> CircuitBreaker:
    global _mongo_breaker
    if _mongo_breaker is None:
        with _breaker_lock:
            if _mongo_breaker is None:
                import os
                _mongo_breaker = CircuitBreaker(
                    name="mongodb",
                    failure_threshold=int(os.getenv("CB_MONGO_FAILURE_THRESHOLD", "5")),
                    recovery_timeout=float(os.getenv("CB_MONGO_RECOVERY_TIMEOUT", "30.0")),
                    success_threshold=2,
                )
    return _mongo_breaker
