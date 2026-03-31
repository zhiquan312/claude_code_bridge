"""Provider health helpers: error classification, backoff, and circuit breaker."""
from __future__ import annotations

import enum
import os
import random
import threading
import time
from typing import Dict, Optional


class ErrorKind(enum.Enum):
    SUCCESS = "success"
    TRANSIENT = "transient"
    PERMANENT = "permanent"


_TRANSIENT_EXIT_CODES = {124, 142}

_PERMANENT_PATTERNS = [
    "unauthorized",
    "forbidden",
    "invalid api key",
    "invalid token",
    "billing",
    "quota exceeded",
    "403 forbidden",
    "401 unauthorized",
    "model not found",
    "invalid model",
]

_TRANSIENT_PATTERNS = [
    "rate limit",
    "timeout",
    "timed out",
    "connection refused",
    "econnreset",
    "server error",
    "service unavailable",
    "overloaded",
    "502",
    "503",
    "504",
    "500",
    "pane died",
    "session lock timeout",
    "pane not available",
    "terminal backend not available",
    "no active",
]

_SESSION_LOCAL_PATTERNS = [
    "pane died",
    "pane not available",
    "session lock timeout",
    "terminal backend not available",
    "another opencode request",
    "no active",
    "session found",
]


def classify_error(exit_code: int, error_text: str) -> ErrorKind:
    """Classify a provider result into success, transient, or permanent."""
    if exit_code == 0:
        return ErrorKind.SUCCESS
    if exit_code in _TRANSIENT_EXIT_CODES:
        return ErrorKind.TRANSIENT

    lower = (error_text or "").lower()

    for pattern in _PERMANENT_PATTERNS:
        if pattern in lower:
            return ErrorKind.PERMANENT

    for pattern in _TRANSIENT_PATTERNS:
        if pattern in lower:
            return ErrorKind.TRANSIENT

    return ErrorKind.TRANSIENT


def is_session_local_error(error_text: str) -> bool:
    """Return True for pane/session-local failures that should not trip the breaker."""
    lower = (error_text or "").lower()
    return any(pattern in lower for pattern in _SESSION_LOCAL_PATTERNS)


def compute_backoff(
    attempt: int,
    base_s: float = 1.0,
    max_s: float = 30.0,
    jitter: bool = True,
) -> float:
    """Compute exponential backoff with optional bounded jitter."""
    delay = min(base_s * (2 ** (attempt - 1)), max_s)
    if jitter:
        delay += random.uniform(0, 0.25 * delay)
    return delay


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Thread-safe per-provider circuit breaker with a single half-open probe."""

    def __init__(self, provider: str, failure_threshold: int = 3, cooldown_s: float = 300.0):
        self.provider = provider
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: Optional[float] = None
        self._last_error = ""
        self._probe_in_flight = False

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._compute_state()

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def _compute_state(self) -> CircuitState:
        if (
            self._state == CircuitState.OPEN
            and self._opened_at is not None
            and (time.time() - self._opened_at) >= self.cooldown_s
        ):
            if self._probe_in_flight:
                return CircuitState.OPEN
            return CircuitState.HALF_OPEN
        return self._state

    def is_available(self) -> bool:
        return self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def try_acquire_probe(self) -> bool:
        with self._lock:
            if self._compute_state() != CircuitState.HALF_OPEN:
                return False
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def release_probe(self) -> None:
        """Release probe lease without recording success or failure.

        Used when the probe request never produced an adapter result
        (daemon timeout, submit failure, cancellation). Without this,
        _probe_in_flight stays True and the provider is wedged in OPEN.
        """
        with self._lock:
            self._probe_in_flight = False

    def record_success(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._opened_at = None
            self._last_error = ""
            self._probe_in_flight = False

    def record_failure(self, kind: ErrorKind, error_text: str = "") -> None:
        with self._lock:
            self._probe_in_flight = False
            self._last_error = error_text or kind.value
            if kind == ErrorKind.PERMANENT:
                self._state = CircuitState.OPEN
                self._failure_count = self.failure_threshold
                self._opened_at = time.time()
                return

            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.time()

    def status_dict(self) -> dict:
        return {
            "provider": self.provider,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "threshold": self.failure_threshold,
            "cooldown_s": self.cooldown_s,
            "last_error": self._last_error,
        }


class ProviderHealthManager:
    """Create and manage per-provider circuit breakers on demand."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._breakers: Dict[str, CircuitBreaker] = {}

    def get_breaker(self, provider: str) -> CircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(provider)
            if breaker is None:
                threshold = int(os.environ.get("CCB_CB_FAILURE_THRESHOLD", "3"))
                cooldown = float(os.environ.get("CCB_CB_COOLDOWN_S", "300"))
                breaker = CircuitBreaker(
                    provider,
                    failure_threshold=threshold,
                    cooldown_s=cooldown,
                )
                self._breakers[provider] = breaker
            return breaker

    def record_result(self, provider: str, exit_code: int, error_text: str = "") -> None:
        kind = classify_error(exit_code, error_text)
        breaker = self.get_breaker(provider)
        if kind == ErrorKind.SUCCESS:
            breaker.record_success()
            return
        if is_session_local_error(error_text):
            return
        breaker.record_failure(kind, error_text)

    def is_available(self, provider: str) -> bool:
        return self.get_breaker(provider).is_available()

    def try_acquire_probe(self, provider: str) -> bool:
        return self.get_breaker(provider).try_acquire_probe()

    def release_probe(self, provider: str) -> None:
        self.get_breaker(provider).release_probe()

    def status_all(self) -> list[dict]:
        with self._lock:
            providers = list(self._breakers)
        return [self.get_breaker(provider).status_dict() for provider in providers]
