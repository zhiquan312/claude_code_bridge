"""
Pane Input Handler for CCB Mail System.

Handles user email replies and sends input to the corresponding AI pane.
"""

import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional, Dict, Any

from .config import MailConfig, SUPPORTED_PROVIDERS
from .filters import filter_incoming


def _extract_email(addr: str) -> str:
    """Extract pure email address from a potentially MIME-encoded address.

    Examples:
        '=?utf-8?B?5YiY5Yip?= <user@example.com>' -> 'user@example.com'
        'John Doe <john@example.com>' -> 'john@example.com'
        'john@example.com' -> 'john@example.com'
    """
    # Use email.utils.parseaddr to extract the email part
    _, email = parseaddr(addr)
    return email.strip().lower() if email else addr.strip().lower()


@dataclass
class InputResult:
    """Result of handling an input."""
    success: bool
    provider: Optional[str] = None
    message: str = ""


class PaneInputHandler:
    """Handle user email replies and send input to panes."""

    def __init__(self, config: MailConfig):
        self.config = config
        self._backend_cache: Dict[str, Any] = {}
        self._pane_id_cache: Dict[str, str] = {}

    def _parse_provider_from_subject(self, subject: str) -> Optional[str]:
        """Parse provider name from email subject."""
        subject_lower = subject.lower()

        # Check for [CCB] Provider Output pattern
        match = re.search(r"\[ccb\]\s*(\w+)\s*output", subject_lower)
        if match:
            provider = match.group(1)
            if provider in SUPPORTED_PROVIDERS:
                return provider

        # Check for Re: [CCB] Provider Output pattern
        match = re.search(r"re:\s*\[ccb\]\s*(\w+)", subject_lower)
        if match:
            provider = match.group(1)
            if provider in SUPPORTED_PROVIDERS:
                return provider

        # Check for [CCB] Provider @ project pattern
        match = re.search(r"\[ccb\]\s*(\w+)\s*@", subject_lower)
        if match:
            provider = match.group(1)
            if provider in SUPPORTED_PROVIDERS:
                return provider

        # Check for provider name anywhere in subject
        for provider in SUPPORTED_PROVIDERS:
            if provider in subject_lower:
                return provider

        return None

    def _parse_project_from_subject(self, subject: str) -> Optional[str]:
        """Parse project name from email subject.

        Subject format: [CCB] Provider @ project_name
        """
        # Match @ followed by project name
        match = re.search(r"@\s*(\w+)", subject)
        if match:
            return match.group(1)
        return None

    def _parse_provider_from_thread_id(self, thread_id: str) -> Optional[str]:
        """Parse provider name from thread ID."""
        if not thread_id:
            return None

        # Thread ID format: ccb-{provider}-{timestamp}
        match = re.search(r"ccb-(\w+)-\d+", thread_id.lower())
        if match:
            provider = match.group(1)
            if provider in SUPPORTED_PROVIDERS:
                return provider

        return None

    def _get_backend(self, provider: str):
        """Get terminal backend for a provider."""
        if provider in self._backend_cache:
            return self._backend_cache[provider]

        try:
            # Try relative import first, then absolute import
            try:
                from ..terminal import TmuxBackend
            except ImportError:
                from lib.terminal import TmuxBackend
            backend = TmuxBackend()
            self._backend_cache[provider] = backend
            return backend
        except Exception as e:
            print(f"[pane_input] Failed to create TmuxBackend: {e}")
            return None

    def _find_pane_by_project(self, provider: str, project_name: str) -> Optional[str]:
        """Find pane ID by provider and project name.

        Matches pane's working directory against project name.
        """
        if not project_name:
            return None

        backend = self._get_backend(provider)
        if not backend:
            return None

        try:
            # List all panes and find one matching provider title and project directory
            import subprocess
            result = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{pane_title}\t#{pane_current_path}"],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode != 0:
                return None

            # Provider title patterns
            if provider == "claude":
                title_patterns = ["Claude Code", "CCB-Claude"]
            elif provider == "codex":
                title_patterns = ["CCB-Codex"]
            elif provider == "droid":
                title_patterns = ["CCB-Droid"]
            elif provider == "opencode":
                title_patterns = ["OpenCode", "OC |", "CCB-Opencode"]
            elif provider == "gemini":
                title_patterns = ["Ready", "Thinking", "CCB-Gemini"]
            else:
                title_patterns = [f"CCB-{provider.capitalize()}"]

            project_lower = project_name.lower()
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                pane_id, title, cwd = parts[0], parts[1], parts[2]

                # Check if title matches provider
                title_match = any(p in title for p in title_patterns)
                if not title_match:
                    continue

                # Check if working directory contains project name
                if project_lower in cwd.lower():
                    return pane_id

        except Exception as e:
            print(f"[pane_input] Error finding pane by project: {e}")

        return None

    def _get_pane_id(self, provider: str, project_name: Optional[str] = None) -> Optional[str]:
        """Get pane ID for a provider, optionally filtered by project name."""
        # Use project-specific cache key if project_name is provided
        cache_key = f"{provider}:{project_name}" if project_name else provider
        if cache_key in self._pane_id_cache:
            return self._pane_id_cache[cache_key]

        # Try to find pane by project name first (most specific)
        if project_name:
            pane_id = self._find_pane_by_project(provider, project_name)
            if pane_id:
                self._pane_id_cache[cache_key] = pane_id
                return pane_id

        # Try to load from pane_ids.json
        try:
            from .daemon import get_pane_ids
            saved_ids = get_pane_ids()
            if provider in saved_ids:
                pane_id = saved_ids[provider]
                if pane_id:
                    self._pane_id_cache[cache_key] = pane_id
                    return pane_id
        except Exception:
            pass

        # Try to find pane by title marker (multiple patterns)
        try:
            backend = self._get_backend(provider)
            if backend:
                # Provider name variants for title matching
                provider_cap = provider.capitalize()  # claude -> Claude

                # Title patterns to try (in order of preference)
                title_patterns = [
                    f"CCB-{provider_cap}",  # CCB-Claude, CCB-Codex
                    f"CCB-{provider}",      # CCB-claude (legacy)
                ]

                # Special patterns for specific providers
                if provider == "claude":
                    title_patterns.extend([
                        "Claude Code",      # Claude Code's actual title
                        "✳ Claude Code",    # With status icon
                        "⠂ Claude Code",    # With spinner icon
                        "◇ Claude Code",    # With idle icon
                    ])
                elif provider == "opencode":
                    title_patterns.extend([
                        "OpenCode",
                        "OC |",             # OpenCode status prefix
                    ])
                elif provider == "gemini":
                    title_patterns.extend([
                        "◇  Ready",         # Gemini CLI idle state
                        "⠋  Thinking",      # Gemini CLI thinking state
                        "Gemini",           # Generic
                    ])

                for pattern in title_patterns:
                    pane_id = backend.find_pane_by_title_marker(pattern)
                    if pane_id:
                        self._pane_id_cache[provider] = pane_id
                        return pane_id
        except Exception:
            pass

        return None

    def set_pane_id(self, provider: str, pane_id: str) -> None:
        """Set pane ID for a provider."""
        self._pane_id_cache[provider] = pane_id

    def handle_reply(
        self,
        from_addr: str,
        subject: str,
        body: str,
        thread_id: Optional[str] = None,
    ) -> InputResult:
        """
        Handle a user email reply.

        Args:
            from_addr: Sender email address
            subject: Email subject
            body: Email body text
            thread_id: Optional thread ID for routing

        Returns:
            InputResult with success status and details
        """
        # Verify sender - extract pure email address for comparison
        sender_email = _extract_email(from_addr)
        target_email = _extract_email(self.config.target_email)
        if sender_email != target_email:
            return InputResult(
                success=False,
                message=f"Sender {sender_email} not authorized (expected {target_email})",
            )

        # Parse provider from thread ID or subject
        provider = self._parse_provider_from_thread_id(thread_id)
        if not provider:
            provider = self._parse_provider_from_subject(subject)

        if not provider:
            return InputResult(
                success=False,
                message="Could not determine target provider from email",
            )

        # Check if hook is enabled for this provider
        hook = self.config.get_hook(provider)
        if not hook or not hook.enabled:
            return InputResult(
                success=False,
                provider=provider,
                message=f"Mail hook not enabled for {provider}",
            )

        # Parse project name from subject for pane matching
        project_name = self._parse_project_from_subject(subject)

        # Get backend and pane ID
        backend = self._get_backend(provider)
        if not backend:
            return InputResult(
                success=False,
                provider=provider,
                message="Terminal backend not available",
            )

        pane_id = self._get_pane_id(provider, project_name)
        if not pane_id:
            return InputResult(
                success=False,
                provider=provider,
                message=f"Pane not found for {provider}" + (f" @ {project_name}" if project_name else ""),
            )

        # Clean up body (remove quoted text, signatures) and filter
        clean_body = self._clean_reply_body(body)

        # Apply security filter
        filter_result = filter_incoming(clean_body)
        if not filter_result.passed:
            return InputResult(
                success=False,
                provider=provider,
                message=f"Content blocked: {filter_result.blocked_reason}",
            )

        clean_body = filter_result.content
        if not clean_body.strip():
            return InputResult(
                success=False,
                provider=provider,
                message="Empty reply body",
            )

        # Send input to pane
        try:
            backend.send_text(pane_id, clean_body)
            return InputResult(
                success=True,
                provider=provider,
                message=f"Input sent to {provider} pane",
            )
        except Exception as e:
            return InputResult(
                success=False,
                provider=provider,
                message=f"Failed to send input: {e}",
            )

    def _clean_reply_body(self, body: str) -> str:
        """Clean up reply body by removing quoted text and signatures."""
        lines = body.splitlines()
        clean_lines = []

        for line in lines:
            # Stop at quoted text markers
            if line.startswith(">"):
                break
            if line.strip().startswith("On ") and " wrote:" in line:
                break
            if line.strip() == "---":
                break
            if line.strip().startswith("Sent via CCB"):
                break
            # Stop at common signature markers
            if line.strip() in ("--", "━" * 10):
                break

            clean_lines.append(line)

        return "\n".join(clean_lines).strip()
