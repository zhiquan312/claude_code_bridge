from __future__ import annotations

import os
import time

from provider_health import (
    CircuitBreaker,
    CircuitState,
    ErrorKind,
    ProviderHealthManager,
    classify_error,
    compute_backoff,
    is_session_local_error,
)


def test_exit_zero_is_success() -> None:
    assert classify_error(0, "") == ErrorKind.SUCCESS


def test_timeout_exit_is_transient() -> None:
    assert classify_error(124, "") == ErrorKind.TRANSIENT


def test_rate_limit_is_transient() -> None:
    assert classify_error(1, "rate limit exceeded") == ErrorKind.TRANSIENT


def test_connection_refused_is_transient() -> None:
    assert classify_error(1, "connection refused") == ErrorKind.TRANSIENT


def test_auth_error_is_permanent() -> None:
    assert classify_error(1, "unauthorized: invalid API key") == ErrorKind.PERMANENT


def test_quota_is_permanent() -> None:
    assert classify_error(1, "quota exceeded") == ErrorKind.PERMANENT


def test_unknown_defaults_to_transient() -> None:
    assert classify_error(1, "something weird") == ErrorKind.TRANSIENT


def test_pane_died_is_transient() -> None:
    assert classify_error(1, "pane died during request") == ErrorKind.TRANSIENT


def test_pane_died_is_session_local() -> None:
    assert is_session_local_error("pane died during request")


def test_session_lock_is_session_local() -> None:
    assert is_session_local_error("session lock timeout")


def test_api_error_not_session_local() -> None:
    assert not is_session_local_error("unauthorized: invalid API key")


def test_backoff_first() -> None:
    assert compute_backoff(1, jitter=False) == 1.0


def test_backoff_second() -> None:
    assert compute_backoff(2, jitter=False) == 2.0


def test_backoff_capped() -> None:
    assert compute_backoff(10, max_s=30.0, jitter=False) == 30.0


def test_backoff_jitter_bounded() -> None:
    for _ in range(100):
        delay = compute_backoff(3, jitter=True)
        assert 0 <= delay <= 4.0 * 1.25


def test_starts_closed() -> None:
    cb = CircuitBreaker("test", failure_threshold=3, cooldown_s=60)
    assert cb.state == CircuitState.CLOSED
    assert cb.is_available()


def test_opens_after_threshold() -> None:
    cb = CircuitBreaker("test", failure_threshold=3, cooldown_s=60)
    cb.record_failure(ErrorKind.TRANSIENT)
    cb.record_failure(ErrorKind.TRANSIENT)
    assert cb.state == CircuitState.CLOSED
    cb.record_failure(ErrorKind.TRANSIENT)
    assert cb.state == CircuitState.OPEN
    assert not cb.is_available()


def test_opens_immediately_on_permanent() -> None:
    cb = CircuitBreaker("test", failure_threshold=3, cooldown_s=60)
    cb.record_failure(ErrorKind.PERMANENT)
    assert cb.state == CircuitState.OPEN


def test_resets_on_success() -> None:
    cb = CircuitBreaker("test", failure_threshold=3, cooldown_s=60)
    cb.record_failure(ErrorKind.TRANSIENT)
    cb.record_failure(ErrorKind.TRANSIENT)
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


def test_half_open_after_cooldown() -> None:
    cb = CircuitBreaker("test", failure_threshold=3, cooldown_s=1)
    for _ in range(3):
        cb.record_failure(ErrorKind.TRANSIENT)
    cb._opened_at = time.time() - 2
    assert cb.state == CircuitState.HALF_OPEN


def test_single_probe_lease() -> None:
    cb = CircuitBreaker("test", failure_threshold=3, cooldown_s=1)
    for _ in range(3):
        cb.record_failure(ErrorKind.TRANSIENT)
    cb._opened_at = time.time() - 2
    assert cb.try_acquire_probe() is True
    assert cb.try_acquire_probe() is False
    assert not cb.is_available()


def test_probe_success_closes() -> None:
    cb = CircuitBreaker("test", failure_threshold=3, cooldown_s=1)
    for _ in range(3):
        cb.record_failure(ErrorKind.TRANSIENT)
    cb._opened_at = time.time() - 2
    assert cb.try_acquire_probe() is True
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


def test_probe_failure_reopens() -> None:
    cb = CircuitBreaker("test", failure_threshold=3, cooldown_s=1)
    for _ in range(3):
        cb.record_failure(ErrorKind.TRANSIENT)
    cb._opened_at = time.time() - 2
    assert cb.try_acquire_probe() is True
    cb.record_failure(ErrorKind.TRANSIENT)
    assert cb.state == CircuitState.OPEN
    assert cb._opened_at is not None
    assert cb._opened_at > time.time() - 1


def test_health_manager_excludes_session_local() -> None:
    mgr = ProviderHealthManager()
    for _ in range(5):
        mgr.record_result("codex", 1, "pane died during request")
    assert mgr.is_available("codex")


def test_release_probe_prevents_wedge() -> None:
    """Releasing probe without success/failure allows future probes."""
    cb = CircuitBreaker("test", failure_threshold=3, cooldown_s=1)
    for _ in range(3):
        cb.record_failure(ErrorKind.TRANSIENT)
    cb._opened_at = time.time() - 2
    assert cb.try_acquire_probe() is True
    # Simulate daemon timeout — release probe without recording result
    cb.release_probe()
    # Should be able to acquire again after next cooldown
    assert cb.try_acquire_probe() is True


def test_health_manager_trips_on_api_failure() -> None:
    old_threshold = os.environ.get("CCB_CB_FAILURE_THRESHOLD")
    try:
        os.environ["CCB_CB_FAILURE_THRESHOLD"] = "3"
        mgr = ProviderHealthManager()
        for _ in range(3):
            mgr.record_result("gemini", 1, "connection refused")
        assert not mgr.is_available("gemini")
    finally:
        if old_threshold is None:
            os.environ.pop("CCB_CB_FAILURE_THRESHOLD", None)
        else:
            os.environ["CCB_CB_FAILURE_THRESHOLD"] = old_threshold
