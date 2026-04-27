"""
Unit tests for utils/circuit_breaker.py

Tests: state transitions, thread safety, fallback behaviour, reset.
"""
import sys
import os
import time
import threading
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.circuit_breaker import CircuitBreaker, State


def _always_fails():
    raise RuntimeError("simulated failure")


def _always_succeeds():
    return "ok"


class TestCircuitBreakerStateTransitions:
    def test_starts_closed(self):
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60)
        assert cb.state == State.CLOSED

    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60)
        for _ in range(3):
            cb.call(_always_fails, fallback=None)
        assert cb.state == State.OPEN

    def test_not_open_below_threshold(self):
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60)
        for _ in range(2):
            cb.call(_always_fails, fallback=None)
        assert cb.state == State.CLOSED

    def test_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.1)
        for _ in range(2):
            cb.call(_always_fails, fallback=None)
        assert cb.state == State.OPEN
        time.sleep(0.15)
        assert cb.state == State.HALF_OPEN

    def test_transitions_to_closed_after_successes_in_half_open(self):
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.1, success_threshold=2)
        for _ in range(2):
            cb.call(_always_fails, fallback=None)
        time.sleep(0.15)
        assert cb.state == State.HALF_OPEN
        cb.call(_always_succeeds, fallback=None)
        cb.call(_always_succeeds, fallback=None)
        assert cb.state == State.CLOSED

    def test_failure_in_half_open_reopens_circuit(self):
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.1, success_threshold=3)
        for _ in range(2):
            cb.call(_always_fails, fallback=None)
        time.sleep(0.15)
        assert cb.state == State.HALF_OPEN
        cb.call(_always_fails, fallback=None)
        assert cb.state == State.OPEN

    def test_reset_restores_closed_state(self):
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=60)
        for _ in range(2):
            cb.call(_always_fails, fallback=None)
        assert cb.state == State.OPEN
        cb.reset()
        assert cb.state == State.CLOSED


class TestCircuitBreakerFallback:
    def test_open_circuit_returns_static_fallback(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=60)
        cb.call(_always_fails, fallback=None)
        result = cb.call(_always_succeeds, fallback="fallback_value")
        assert result == "fallback_value"

    def test_open_circuit_calls_callable_fallback(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=60)
        cb.call(_always_fails, fallback=None)
        result = cb.call(_always_succeeds, fallback=lambda: "computed_fallback")
        assert result == "computed_fallback"

    def test_closed_circuit_returns_function_result(self):
        cb = CircuitBreaker("test", failure_threshold=5, recovery_timeout=60)
        result = cb.call(_always_succeeds, fallback="not_used")
        assert result == "ok"

    def test_failure_returns_fallback_immediately(self):
        cb = CircuitBreaker("test", failure_threshold=5, recovery_timeout=60)
        result = cb.call(_always_fails, fallback="fallback_on_failure")
        assert result == "fallback_on_failure"


class TestCircuitBreakerIsAvailable:
    def test_available_when_closed(self):
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60)
        assert cb.is_available() is True

    def test_unavailable_when_open(self):
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=60)
        for _ in range(2):
            cb.call(_always_fails, fallback=None)
        assert cb.is_available() is False


class TestCircuitBreakerGetStats:
    def test_stats_returned(self):
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60)
        stats = cb.get_stats()
        assert stats["name"] == "test"
        assert stats["state"] == "closed"
        assert "failure_count" in stats

    def test_stats_reflect_failures(self):
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60)
        cb.call(_always_fails, fallback=None)
        stats = cb.get_stats()
        assert stats["failure_count"] == 1


class TestCircuitBreakerThreadSafety:
    def test_concurrent_failures_dont_exceed_threshold(self):
        cb = CircuitBreaker("test", failure_threshold=5, recovery_timeout=60)
        errors = []

        def hammer():
            try:
                for _ in range(10):
                    cb.call(_always_fails, fallback=None)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=hammer) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # State should be OPEN (threshold exceeded by concurrent calls)
        assert cb.state == State.OPEN
