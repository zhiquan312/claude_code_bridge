from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
BIN_DIR = SCRIPT_DIR.parent / "bin"
ASKD_BIN = BIN_DIR / "askd"
CCB_PING_BIN = BIN_DIR / "ccb-ping"
CCB_MOUNTED_BIN = BIN_DIR / "ccb-mounted"


def _write_gemini_session(project_dir: Path) -> Path:
    cfg_dir = project_dir / ".ccb"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    session_file = cfg_dir / ".gemini-session"
    payload = {
        "active": True,
        "work_dir": str(project_dir),
        "runtime_dir": str(project_dir),
        "session_id": "test-session",
        "pane_id": "%1",
        "terminal": "tmux",
    }
    session_file.write_text(json.dumps(payload, ensure_ascii=True) + "\n", encoding="utf-8")
    return session_file


def _shutdown_askd(run_dir: Path) -> None:
    env = dict(os.environ)
    env["CCB_RUN_DIR"] = str(run_dir)
    subprocess.run([str(ASKD_BIN), "--shutdown"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_ccb_ping_autostart_uses_session_file_work_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    other_dir = tmp_path / "other"
    run_dir = tmp_path / "run"
    project_dir.mkdir()
    other_dir.mkdir()
    run_dir.mkdir()
    session_file = _write_gemini_session(project_dir)

    env = dict(os.environ)
    env["CCB_GASKD"] = "1"
    env["CCB_GASKD_AUTOSTART"] = "1"
    env["CCB_RUN_DIR"] = str(run_dir)

    try:
        subprocess.run(
            [str(CCB_PING_BIN), "gemini", "--session-file", str(session_file), "--autostart"],
            cwd=str(other_dir),
            env=env,
            capture_output=True,
            text=True,
        )
        assert (run_dir / "askd.json").exists()
    finally:
        _shutdown_askd(run_dir)


def test_ccb_mounted_autostart_uses_target_path(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    other_dir = tmp_path / "other"
    run_dir = tmp_path / "run"
    project_dir.mkdir()
    other_dir.mkdir()
    run_dir.mkdir()
    _write_gemini_session(project_dir)

    env = dict(os.environ)
    env["CCB_GASKD"] = "1"
    env["CCB_GASKD_AUTOSTART"] = "1"
    env["CCB_RUN_DIR"] = str(run_dir)

    try:
        result = subprocess.run(
            ["bash", str(CCB_MOUNTED_BIN), "--autostart", str(project_dir)],
            cwd=str(other_dir),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert parsed.get("cwd") == str(project_dir)
        assert isinstance(parsed.get("mounted"), list)
        assert (run_dir / "askd.json").exists()
    finally:
        _shutdown_askd(run_dir)


def test_ccb_mounted_does_not_false_positive_on_unhealthy_session(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    other_dir = tmp_path / "other"
    run_dir = tmp_path / "run"
    project_dir.mkdir()
    other_dir.mkdir()
    run_dir.mkdir()
    session_file = _write_gemini_session(project_dir)

    env = dict(os.environ)
    env["CCB_GASKD"] = "1"
    env["CCB_GASKD_AUTOSTART"] = "1"
    env["CCB_RUN_DIR"] = str(run_dir)

    try:
        ping_result = subprocess.run(
            [str(CCB_PING_BIN), "gemini", "--session-file", str(session_file), "--autostart"],
            cwd=str(other_dir),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        # Autostart may start askd, but health still fails because the synthetic
        # session points at a fake tmux pane. Mounted must not report it online
        # just because unified askd is running.
        assert ping_result.returncode != 0
        assert (run_dir / "askd.json").exists()

        result = subprocess.run(
            ["bash", str(CCB_MOUNTED_BIN), str(project_dir)],
            cwd=str(other_dir),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
        parsed = json.loads(result.stdout)
        assert "gemini" not in parsed.get("mounted", [])
    finally:
        _shutdown_askd(run_dir)
