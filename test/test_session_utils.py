from __future__ import annotations

from pathlib import Path

from session_utils import find_project_session_file, safe_write_session


def test_find_project_session_file_walks_upward(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    leaf = root / "a" / "b" / "c"
    leaf.mkdir(parents=True)

    session = root / ".codex-session"
    session.write_text("{}", encoding="utf-8")

    found = find_project_session_file(leaf, ".codex-session")
    assert found == session
    assert find_project_session_file(root, ".codex-session") == session


def test_find_project_session_file_prefers_ccb_config(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir(parents=True)

    cfg = root / ".ccb"
    cfg.mkdir(parents=True)
    primary = cfg / ".codex-session"
    primary.write_text("{}", encoding="utf-8")

    legacy = root / ".codex-session"
    legacy.write_text("{}", encoding="utf-8")

    assert find_project_session_file(root, ".codex-session") == primary


def test_find_project_session_file_stops_at_ccb_boundary(tmp_path: Path) -> None:
    """Session walk must stop at .ccb/ boundary to prevent cross-project leakage.

    Regression: without the boundary check, a subdirectory inside project A
    would inherit project B's session if B was a parent directory with .ccb/.
    """
    # Parent project with its own .ccb/ and a claude session
    parent_project = tmp_path / "parent"
    parent_project.mkdir()
    parent_ccb = parent_project / ".ccb"
    parent_ccb.mkdir()
    (parent_ccb / ".claude-session").write_text('{"pane_id":"parent-pane"}', encoding="utf-8")

    # Child project inside parent, has its own .ccb/ but NO claude session
    child_project = parent_project / "child"
    child_project.mkdir()
    child_ccb = child_project / ".ccb"
    child_ccb.mkdir()

    subdir = child_project / "src" / "lib"
    subdir.mkdir(parents=True)

    # From child's subdir, should NOT find parent's session (boundary stops walk)
    found = find_project_session_file(subdir, ".claude-session")
    assert found is None, f"Session leaked from parent project: {found}"


def test_find_project_session_file_finds_own_session_in_subdir(tmp_path: Path) -> None:
    """Subdirectory should find its own project's session, not a parent's."""
    project = tmp_path / "myproject"
    project.mkdir()
    ccb = project / ".ccb"
    ccb.mkdir()
    (ccb / ".claude-session").write_text('{"pane_id":"my-pane"}', encoding="utf-8")

    subdir = project / "src" / "components"
    subdir.mkdir(parents=True)

    found = find_project_session_file(subdir, ".claude-session")
    assert found == ccb / ".claude-session"


def test_safe_write_session_atomic_write(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    ok, err = safe_write_session(target, '{"hello":"world"}\n')
    assert ok is True
    assert err is None
    assert target.read_text(encoding="utf-8") == '{"hello":"world"}\n'
    assert not target.with_suffix(".tmp").exists()

    ok2, err2 = safe_write_session(target, '{"hello":"again"}\n')
    assert ok2 is True
    assert err2 is None
    assert target.read_text(encoding="utf-8") == '{"hello":"again"}\n'
    assert not target.with_suffix(".tmp").exists()
