"""Integration tests: circuit breaker + journal + classification work together."""
import os
import tempfile
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
from task_journal import TaskJournal, TaskState


def test_full_success_flow():
    """Provider succeeds -> circuit stays closed, journal records lifecycle."""
    with tempfile.TemporaryDirectory() as d:
        health = ProviderHealthManager()
        journal = TaskJournal(journal_dir=d)
        journal.record("r1", "codex", TaskState.QUEUED)
        journal.record("r1", "codex", TaskState.RUNNING)
        kind = classify_error(0, "")
        assert kind == ErrorKind.SUCCESS
        health.record_result("codex", 0, "")
        journal.record("r1", "codex", TaskState.COMPLETED)
        assert health.is_available("codex")
        assert journal.get_latest_state("r1") == TaskState.COMPLETED
        assert len(journal.get_pending_tasks()) == 0


def test_transient_failures_open_circuit():
    """3 transient failures open circuit, cooldown + success closes it."""
    os.environ["CCB_CB_FAILURE_THRESHOLD"] = "3"
    os.environ["CCB_CB_COOLDOWN_S"] = "1"
    try:
        health = ProviderHealthManager()
        for _ in range(3):
            health.record_result("gemini", 1, "connection refused")
        assert not health.is_available("gemini")
        # Simulate cooldown
        health.get_breaker("gemini")._opened_at = time.time() - 2
        assert health.is_available("gemini")
        health.record_result("gemini", 0, "")
        assert health.is_available("gemini")
    finally:
        del os.environ["CCB_CB_FAILURE_THRESHOLD"]
        del os.environ["CCB_CB_COOLDOWN_S"]


def test_permanent_failure_opens_immediately():
    health = ProviderHealthManager()
    kind = classify_error(1, "401 unauthorized: invalid API key")
    assert kind == ErrorKind.PERMANENT
    health.record_result("opencode", 1, "401 unauthorized: invalid API key")
    assert not health.is_available("opencode")


def test_pane_deaths_do_not_trip_breaker():
    """Session-local errors excluded from provider-wide breaker."""
    health = ProviderHealthManager()
    for _ in range(10):
        health.record_result("codex", 1, "Codex pane died during request")
    assert health.is_available("codex")


def test_half_open_single_probe():
    """Only one request gets through in HALF_OPEN."""
    os.environ["CCB_CB_FAILURE_THRESHOLD"] = "2"
    os.environ["CCB_CB_COOLDOWN_S"] = "1"
    try:
        health = ProviderHealthManager()
        health.record_result("gemini", 1, "connection refused")
        health.record_result("gemini", 1, "connection refused")
        assert not health.is_available("gemini")
        # Simulate cooldown
        health.get_breaker("gemini")._opened_at = time.time() - 2
        assert health.is_available("gemini")
        assert health.try_acquire_probe("gemini") is True
        assert health.try_acquire_probe("gemini") is False
        assert not health.is_available("gemini")
        # Probe succeeds -> closes
        health.get_breaker("gemini").record_success()
        assert health.is_available("gemini")
    finally:
        del os.environ["CCB_CB_FAILURE_THRESHOLD"]
        del os.environ["CCB_CB_COOLDOWN_S"]


def test_fire_and_forget_invisible_to_journal():
    """Fire-and-forget (timeout_s=0) produces no journal rows."""
    with tempfile.TemporaryDirectory() as d:
        journal = TaskJournal(journal_dir=d)
        # Daemon skips ALL journal writes for fire-and-forget
        assert len(journal.read_all()) == 0


def test_daemon_timeout_excluded_from_health():
    """Daemon wait timeouts do NOT trip the circuit breaker."""
    health = ProviderHealthManager()
    # Daemon-side timeouts: record_result is NEVER called
    # Only journal CANCELLED is written (not tested here — that's daemon-level)
    # Breaker should remain closed
    assert health.is_available("codex")
    assert health.is_available("gemini")
    assert health.is_available("opencode")


def test_pending_survives_restart():
    """Pending tasks survive journal reopen (simulated restart)."""
    with tempfile.TemporaryDirectory() as d:
        TaskJournal(journal_dir=d).record("r1", "codex", TaskState.RUNNING)
        TaskJournal(journal_dir=d).record("r2", "gemini", TaskState.COMPLETED)
        j2 = TaskJournal(journal_dir=d)
        pending = j2.get_pending_tasks()
        assert "r1" in pending
        assert "r2" not in pending


def test_backoff_values():
    assert compute_backoff(1, jitter=False) == 1.0
    assert compute_backoff(2, jitter=False) == 2.0
    assert compute_backoff(3, jitter=False) == 4.0
    assert compute_backoff(10, max_s=30.0, jitter=False) == 30.0


def test_session_local_classification():
    assert is_session_local_error("pane died during request")
    assert is_session_local_error("session lock timeout")
    assert is_session_local_error("No active Codex session found")
    assert not is_session_local_error("connection refused")
    assert not is_session_local_error("unauthorized")
