from __future__ import annotations

import hashlib
import os
import posixpath
import re
from pathlib import Path


_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:([/\\\\]|$)")
_MNT_DRIVE_RE = re.compile(r"^/mnt/([A-Za-z])/(.*)$")
_MSYS_DRIVE_RE = re.compile(r"^/([A-Za-z])/(.*)$")


def normalize_work_dir(value: str | Path) -> str:
    """
    Normalize a work_dir into a stable string for hashing and matching.

    Goals:
    - Be stable within a single environment (Linux/WSL/Windows/MSYS).
    - Reduce trivial path-format mismatches (slashes, drive letter casing, /mnt/<drive> mapping).
    - Avoid resolve() by default to reduce symlink/interop surprises.
    """
    raw = str(value).strip()
    if not raw:
        return ""

    # Expand "~" early.
    if raw.startswith("~"):
        try:
            raw = os.path.expanduser(raw)
        except Exception:
            pass

    # Absolutize when relative (best-effort).
    try:
        preview = raw.replace("\\", "/")
        is_abs = (
            preview.startswith("/")
            or preview.startswith("//")
            or preview.startswith("\\\\")
            or bool(_WIN_DRIVE_RE.match(preview))
        )
        if not is_abs:
            raw = str((Path.cwd() / Path(raw)).absolute())
    except Exception:
        pass

    s = raw.replace("\\", "/")

    # Map WSL mount paths to a Windows-like drive form for stable matching.
    m = _MNT_DRIVE_RE.match(s)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2)
        s = f"{drive}:/{rest}"
    else:
        # Map MSYS /c/... to c:/...
        m = _MSYS_DRIVE_RE.match(s)
        if m and ("MSYSTEM" in os.environ or os.name == "nt"):
            drive = m.group(1).lower()
            rest = m.group(2)
            s = f"{drive}:/{rest}"

    # Collapse redundant separators and dot segments using POSIX semantics (we forced "/").
    if s.startswith("//"):
        prefix = "//"
        rest = posixpath.normpath(s[2:])
        s = prefix + rest.lstrip("/")
    else:
        s = posixpath.normpath(s)

    # Normalize Windows drive letter casing.
    if _WIN_DRIVE_RE.match(s):
        s = s[0].lower() + s[1:]

    return s


def _find_ccb_config_root(start_dir: Path) -> Path | None:
    """
    Find the nearest ancestor (including *start_dir* itself) that contains a
    `.ccb/` or legacy `.ccb_config/` directory.

    Walking upward ensures that calls from any subdirectory inside a project
    resolve to the same canonical project root.

    The user's home directory is excluded: ``~/.ccb`` is CCB runtime state,
    not a project anchor.  Without this guard, every directory under ``$HOME``
    that lacks its own ``.ccb/`` would collapse to the home directory,
    causing cross-project routing collisions.
    """
    try:
        current = Path(start_dir).expanduser().absolute()
    except Exception:
        current = Path.cwd()

    try:
        home = Path.home()
    except Exception:
        home = None

    try:
        for directory in [current, *current.parents]:
            # Skip $HOME — ~/.ccb is runtime state, not a project anchor.
            if home is not None and directory == home:
                continue
            if (directory / ".ccb").is_dir():
                return directory
            if (directory / ".ccb_config").is_dir():
                return directory
    except Exception:
        return None
    return None


def find_project_root(work_dir: Path) -> Path:
    """Return the canonical project root for *work_dir*.

    Walks up from *work_dir* looking for `.ccb/` or `.ccb_config/`.
    Falls back to *work_dir* itself when no config directory is found.
    """
    try:
        wd = Path(work_dir).expanduser().absolute()
    except Exception:
        wd = Path.cwd()
    root = _find_ccb_config_root(wd)
    return root if root is not None else wd


def compute_ccb_project_id(work_dir: Path) -> str:
    """
    Compute CCB's routing project id (ccb_project_id).

    Priority:
    - Nearest ancestor directory containing `.ccb/` (project anchor).
    - Current work_dir (fallback when no `.ccb/` found).
    """
    try:
        wd = Path(work_dir).expanduser().absolute()
    except Exception:
        wd = Path.cwd()

    # Priority 1: Current directory `.ccb/` only
    base = _find_ccb_config_root(wd)

    if base is None:
        base = wd

    norm = normalize_work_dir(base)
    if not norm:
        norm = normalize_work_dir(wd)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
