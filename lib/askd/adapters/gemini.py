"""
Gemini provider adapter for the unified ask daemon.

Wraps existing gaskd_* modules to provide a consistent interface.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from askd.adapters.base import BaseProviderAdapter, ProviderRequest, ProviderResult, QueuedTask
from askd_runtime import log_path, write_log
from ccb_protocol import normalize_box_wrapped_text
from completion_hook import (
    COMPLETION_STATUS_CANCELLED,
    COMPLETION_STATUS_COMPLETED,
    COMPLETION_STATUS_FAILED,
    COMPLETION_STATUS_INCOMPLETE,
    default_reply_for_status,
    notify_completion,
)
from gaskd_protocol import extract_reply_for_req, is_done_text, wrap_gemini_prompt
from gaskd_session import compute_session_key, load_project_session
from gemini_comm import GeminiLogReader
from providers import GASKD_SPEC
from terminal import get_backend_for_session


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write_log(line: str) -> None:
    write_log(log_path(GASKD_SPEC.log_file_name), line)


_NO_REPLY_TIMEOUT_S = float(os.environ.get("CCB_GEMINI_NO_REPLY_TIMEOUT", "300.0"))
_PROMPT_CHECK_LINES = int(os.environ.get("CCB_GEMINI_PROMPT_CHECK_LINES", "30"))
_PANE_DONE_CHECK_LINES = int(os.environ.get("CCB_GEMINI_DONE_CHECK_LINES", "80"))


def _is_cancel_text(text: str) -> bool:
    s = (text or "").strip().lower()
    if not s:
        return False
    if "request cancelled" in s or "request canceled" in s:
        return True
    return False


def _read_session_messages(session_path: Path) -> Optional[list[dict]]:
    for attempt in range(10):
        try:
            with session_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            messages = data.get("messages", []) if isinstance(data, dict) else []
            return messages if isinstance(messages, list) else []
        except json.JSONDecodeError:
            if attempt < 9:
                time.sleep(0.05)
                continue
            return None
        except Exception:
            return None


def _cancel_applies_to_req(messages: list[dict], cancel_index: int, req_id: str) -> bool:
    needle = f"CCB_REQ_ID: {req_id}"
    for j in range(cancel_index - 1, -1, -1):
        msg = messages[j]
        if not isinstance(msg, dict):
            continue
        if msg.get("type") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            content = str(content or "")
        return needle in content
    return False


def _detect_request_cancelled(session_path: Path, *, from_index: int, req_id: str) -> bool:
    if from_index < 0:
        from_index = 0
    messages = _read_session_messages(session_path)
    if messages is None:
        return False
    for i in range(min(from_index, len(messages)), len(messages)):
        msg = messages[i]
        if not isinstance(msg, dict):
            continue
        if msg.get("type") != "info":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            content = str(content or "")
        if not _is_cancel_text(content):
            continue
        if _cancel_applies_to_req(messages, i, req_id):
            return True
    return False


def _capture_normalized_pane_text(backend: Any, pane_id: str, *, lines: int = _PANE_DONE_CHECK_LINES) -> str:
    try:
        return normalize_box_wrapped_text(backend.get_text(pane_id, lines=lines) or "")
    except Exception:
        return ""


def _pane_done_reply(backend: Any, pane_id: str, req_id: str) -> str:
    pane_text = _capture_normalized_pane_text(backend, pane_id)
    if pane_text and is_done_text(pane_text, req_id):
        return pane_text
    return ""


class GeminiAdapter(BaseProviderAdapter):
    """Adapter for Gemini provider."""

    @property
    def key(self) -> str:
        return "gemini"

    @property
    def spec(self):
        return GASKD_SPEC

    @property
    def session_filename(self) -> str:
        return ".gemini-session"

    def load_session(self, work_dir: Path, instance: Optional[str] = None) -> Optional[Any]:
        return load_project_session(work_dir, instance)

    def compute_session_key(self, session: Any, instance: Optional[str] = None) -> str:
        return compute_session_key(session, instance) if session else "gemini:unknown"

    def handle_task(self, task: QueuedTask) -> ProviderResult:
        started_ms = _now_ms()
        req = task.request
        work_dir = Path(req.work_dir)
        _write_log(f"[INFO] start provider=gemini req_id={task.req_id} work_dir={req.work_dir}")

        instance = task.request.instance
        session = load_project_session(work_dir, instance)
        session_key = self.compute_session_key(session, instance)

        if not session:
            return ProviderResult(
                exit_code=1,
                reply="No active Gemini session found for work_dir.",
                req_id=task.req_id,
                session_key=session_key,
                done_seen=False,
                status=COMPLETION_STATUS_FAILED,
            )

        ok, pane_or_err = session.ensure_pane()
        if not ok:
            return ProviderResult(
                exit_code=1,
                reply=f"Session pane not available: {pane_or_err}",
                req_id=task.req_id,
                session_key=session_key,
                done_seen=False,
                status=COMPLETION_STATUS_FAILED,
            )
        pane_id = pane_or_err

        backend = get_backend_for_session(session.data)
        if not backend:
            return ProviderResult(
                exit_code=1,
                reply="Terminal backend not available",
                req_id=task.req_id,
                session_key=session_key,
                done_seen=False,
                status=COMPLETION_STATUS_FAILED,
            )

        log_reader = GeminiLogReader(work_dir=Path(session.work_dir))
        if session.gemini_session_path:
            try:
                log_reader.set_preferred_session(Path(session.gemini_session_path))
            except Exception:
                pass
        state = log_reader.capture_state()

        prompt = wrap_gemini_prompt(req.message, task.req_id)
        prompt_sent_at = time.time()
        backend.send_text(pane_id, prompt)

        # Verify prompt delivery: check pane received the text, retry once if not
        prompt_verified = False
        time.sleep(0.5)
        try:
            _pane_text = backend.get_text(pane_id, lines=_PROMPT_CHECK_LINES) or ""
            prompt_verified = task.req_id in _pane_text
            if not prompt_verified:
                _write_log(f"[WARN] Prompt may not be delivered, retrying send req_id={task.req_id}")
                backend.send_text(pane_id, prompt)
                time.sleep(0.5)
                _pane_text = backend.get_text(pane_id, lines=_PROMPT_CHECK_LINES) or ""
                prompt_verified = task.req_id in _pane_text
                if not prompt_verified:
                    _write_log(
                        f"[WARN] Prompt still not visible after retry req_id={task.req_id} "
                        f"pane={pane_id}"
                    )
        except Exception:
            pass

        deadline = None if float(req.timeout_s) < 0.0 else (time.time() + float(req.timeout_s))
        done_seen = False
        done_ms: Optional[int] = None
        latest_reply = ""
        request_cancelled = False

        pane_check_interval = float(os.environ.get("CCB_GASKD_PANE_CHECK_INTERVAL", "2.0"))
        last_pane_check = time.time()

        # Soft idle watchdog: log and rescan when Gemini appears stalled, but do
        # not treat idle as completion without a confirmed marker.
        idle_timeout = float(os.environ.get("CCB_GEMINI_IDLE_TIMEOUT", "90.0"))
        _last_reply_snapshot = ""
        _last_reply_changed_at = time.time()
        stale_rescan_s = float(os.environ.get("CCB_GEMINI_STALE_SESSION_RESCAN_S", "10.0"))
        _last_stale_rescan_at = time.time()

        while True:
            # Check for cancellation
            if task.cancel_event and task.cancel_event.is_set():
                _write_log(f"[INFO] Task cancelled during wait loop: req_id={task.req_id}")
                break

            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                wait_step = min(remaining, 1.0)
            else:
                wait_step = 1.0

            if time.time() - last_pane_check >= pane_check_interval:
                try:
                    alive = bool(backend.is_alive(pane_id))
                except Exception:
                    alive = False
                if not alive:
                    _write_log(f"[ERROR] Pane {pane_id} died during request req_id={task.req_id}")
                    return ProviderResult(
                        exit_code=1,
                        reply="Gemini pane died during request",
                        req_id=task.req_id,
                        session_key=session_key,
                        done_seen=False,
                        status=COMPLETION_STATUS_FAILED,
                    )
                last_pane_check = time.time()

            scan_from = state.get("msg_count")
            try:
                scan_from_i = int(scan_from) if scan_from is not None else 0
            except Exception:
                scan_from_i = 0

            prev_session_path = state.get("session_path")
            reply, state = log_reader.wait_for_message(state, wait_step)

            # Detect cancellation
            try:
                current_count = int(state.get("msg_count") or 0)
            except Exception:
                current_count = 0
            session_path = state.get("session_path")
            if isinstance(session_path, Path) and isinstance(prev_session_path, Path):
                if session_path != prev_session_path:
                    scan_from_i = 0
            if isinstance(session_path, Path) and current_count > scan_from_i:
                if _detect_request_cancelled(session_path, from_index=scan_from_i, req_id=task.req_id):
                    _write_log(f"[WARN] Gemini request cancelled req_id={task.req_id}")
                    request_cancelled = True
                    latest_reply = "Gemini request cancelled."
                    break

            if not reply:
                if stale_rescan_s > 0 and (time.time() - _last_stale_rescan_at) >= stale_rescan_s:
                    try:
                        refreshed = log_reader.capture_state()
                        if refreshed.get("session_path") != state.get("session_path"):
                            _write_log(
                                f"[INFO] Gemini session rescan adopted newer log "
                                f"req_id={task.req_id} session={refreshed.get('session_path')}"
                            )
                        state = refreshed
                    except Exception:
                        pass
                    _last_stale_rescan_at = time.time()
                if _NO_REPLY_TIMEOUT_S > 0 and not latest_reply:
                    no_reply_elapsed = time.time() - prompt_sent_at
                    if no_reply_elapsed >= _NO_REPLY_TIMEOUT_S:
                        pane_reply = _pane_done_reply(backend, pane_id, task.req_id)
                        if pane_reply:
                            latest_reply = pane_reply
                            done_seen = True
                            done_ms = _now_ms() - started_ms
                            break
                        if prompt_verified:
                            _write_log(
                                f"[WARN] Gemini produced no reply within {_NO_REPLY_TIMEOUT_S:.1f}s "
                                f"after prompt send req_id={task.req_id} — continuing to wait"
                            )
                        else:
                            _write_log(
                                f"[WARN] Gemini prompt delivery/reply slow after "
                                f"{_NO_REPLY_TIMEOUT_S:.1f}s req_id={task.req_id} pane={pane_id} — continuing to wait"
                            )
                        continue
                continue
            latest_reply = str(reply)
            normalized_reply = normalize_box_wrapped_text(latest_reply)
            if is_done_text(normalized_reply, task.req_id):
                latest_reply = normalized_reply
                done_seen = True
                done_ms = _now_ms() - started_ms
                break

            if normalized_reply != _last_reply_snapshot:
                _last_reply_snapshot = normalized_reply
                _last_reply_changed_at = time.time()
            elif normalized_reply and idle_timeout > 0 and (time.time() - _last_reply_changed_at >= idle_timeout):
                pane_reply = _pane_done_reply(backend, pane_id, task.req_id)
                if pane_reply:
                    latest_reply = pane_reply
                    done_seen = True
                    done_ms = _now_ms() - started_ms
                    break
                _write_log(
                    f"[WARN] Gemini reply idle for {idle_timeout}s without CCB_DONE "
                    f"req_id={task.req_id}; continuing to wait"
                )
                try:
                    state = log_reader.capture_state()
                except Exception:
                    pass
                _last_reply_changed_at = time.time()

        if not done_seen:
            pane_reply = _pane_done_reply(backend, pane_id, task.req_id)
            if pane_reply:
                latest_reply = pane_reply
                done_seen = True
                done_ms = _now_ms() - started_ms

        final_reply = extract_reply_for_req(normalize_box_wrapped_text(latest_reply), task.req_id)
        status = COMPLETION_STATUS_COMPLETED if done_seen else COMPLETION_STATUS_INCOMPLETE
        if request_cancelled or task.cancelled:
            status = COMPLETION_STATUS_CANCELLED
        reply_for_hook = final_reply
        if not reply_for_hook.strip():
            reply_for_hook = default_reply_for_status(status, done_seen=done_seen)
        notify_completion(
            provider="gemini",
            output_file=req.output_path,
            reply=reply_for_hook,
            req_id=task.req_id,
            done_seen=done_seen,
            status=status,
            caller=req.caller,
            email_req_id=req.email_req_id,
            email_msg_id=req.email_msg_id,
            email_from=req.email_from,
            work_dir=req.work_dir,
            caller_pane_id=req.caller_pane_id,
            caller_terminal=req.caller_terminal,
        )

        result = ProviderResult(
            exit_code=0 if done_seen else 2,
            reply=final_reply,
            req_id=task.req_id,
            session_key=session_key,
            done_seen=done_seen,
            done_ms=done_ms,
            status=status,
        )
        _write_log(f"[INFO] done provider=gemini req_id={task.req_id} exit={result.exit_code}")
        return result
