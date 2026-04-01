from __future__ import annotations

import os
from pathlib import Path

import pytest

from project_id import compute_ccb_project_id, find_project_root, normalize_work_dir


def test_normalize_work_dir_basic() -> None:
    # On Windows, /a/... is interpreted as MSYS path (a:/)
    # On Unix, it's a regular absolute path
    result1 = normalize_work_dir("/a/b/../c")
    if os.name == 'nt':
        assert result1 == "a:/c", f"Expected a:/c on Windows, got {result1}"
    else:
        assert result1 == "/a/c", f"Expected /a/c on Unix, got {result1}"

    result2 = normalize_work_dir("/a//b///c")
    if os.name == 'nt':
        assert result2 == "a:/b/c", f"Expected a:/b/c on Windows, got {result2}"
    else:
        assert result2 == "/a/b/c", f"Expected /a/b/c on Unix, got {result2}"


def test_normalize_work_dir_wsl_drive_mapping() -> None:
    assert normalize_work_dir("/mnt/C/Users/alice") == "c:/Users/alice"
    assert normalize_work_dir("/mnt/c/Users/alice") == "c:/Users/alice"


def test_compute_ccb_project_id_stable_for_same_dir(tmp_path: Path) -> None:
    pid1 = compute_ccb_project_id(tmp_path)
    pid2 = compute_ccb_project_id(tmp_path)
    assert pid1
    assert pid1 == pid2


def test_compute_ccb_project_id_uses_anchor_root(tmp_path: Path) -> None:
    (tmp_path / ".ccb").mkdir(parents=True, exist_ok=True)
    subdir = tmp_path / "a" / "b"
    subdir.mkdir(parents=True, exist_ok=True)

    pid_root = compute_ccb_project_id(tmp_path)
    pid_sub = compute_ccb_project_id(subdir)
    assert pid_root
    assert pid_sub
    # Subdirectory walks up to the .ccb/ anchor and gets the same project ID
    assert pid_root == pid_sub


def test_compute_ccb_project_id_ignores_env_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    child = root / "sub"
    child.mkdir(parents=True, exist_ok=True)

    # No anchor: env var should not override current-dir isolation.
    monkeypatch.setenv("CCB_PROJECT_ROOT", str(root))
    pid_root = compute_ccb_project_id(root)
    pid_child = compute_ccb_project_id(child)
    assert pid_root
    assert pid_root != pid_child

    # Invalid env root should not crash.
    monkeypatch.setenv("CCB_PROJECT_ROOT", str(tmp_path / "does-not-exist"))
    assert compute_ccb_project_id(child)


def test_compute_ccb_project_id_fallback_diff_for_subdirs_without_anchor(tmp_path: Path) -> None:
    subdir = tmp_path / "a" / "b"
    subdir.mkdir(parents=True, exist_ok=True)
    assert compute_ccb_project_id(tmp_path) != compute_ccb_project_id(subdir)


# --- Nested-directory tests (ancestor traversal) ---


def test_find_project_root_returns_ccb_ancestor(tmp_path: Path) -> None:
    """find_project_root should walk up and return the nearest .ccb/ ancestor."""
    (tmp_path / ".ccb").mkdir()
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)

    assert find_project_root(tmp_path) == tmp_path
    assert find_project_root(deep) == tmp_path


def test_find_project_root_fallback_when_no_anchor(tmp_path: Path) -> None:
    """Without .ccb/ anywhere, find_project_root falls back to the given dir."""
    subdir = tmp_path / "x" / "y"
    subdir.mkdir(parents=True)
    assert find_project_root(subdir) == subdir


def test_find_project_root_legacy_ccb_config(tmp_path: Path) -> None:
    """Legacy .ccb_config/ is also recognized as a project anchor."""
    (tmp_path / ".ccb_config").mkdir()
    deep = tmp_path / "src" / "lib"
    deep.mkdir(parents=True)

    assert find_project_root(deep) == tmp_path


def test_nested_subdir_same_project_id(tmp_path: Path) -> None:
    """All subdirectories under a .ccb/ project produce the same project ID."""
    (tmp_path / ".ccb").mkdir()
    dirs = [
        tmp_path,
        tmp_path / "src",
        tmp_path / "src" / "lib",
        tmp_path / ".autoflow",
        tmp_path / "test" / "integration",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    root_pid = compute_ccb_project_id(tmp_path)
    for d in dirs[1:]:
        assert compute_ccb_project_id(d) == root_pid, f"{d} got different project ID"


def test_home_dir_ccb_excluded_from_anchor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """~/.ccb is CCB runtime state, not a project anchor.

    Regression: without $HOME exclusion, every directory under $HOME lacking
    its own .ccb/ would collapse to the home directory, causing cross-project
    routing collisions.
    """
    # Simulate $HOME with a .ccb/ directory (runtime state)
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    (fake_home / ".ccb").mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Two separate project dirs under fake $HOME, neither has .ccb/
    proj_a = fake_home / "projects" / "alpha"
    proj_b = fake_home / "projects" / "beta"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)

    # They should NOT collapse to fake_home — they should be independent
    pid_a = compute_ccb_project_id(proj_a)
    pid_b = compute_ccb_project_id(proj_b)
    assert pid_a != pid_b, "Projects under $HOME collapsed to same ID (home anchor not excluded)"

    # find_project_root should return the dirs themselves, not $HOME
    assert find_project_root(proj_a) == proj_a
    assert find_project_root(proj_b) == proj_b


def test_subdir_with_ccb_under_home_resolves_correctly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A project under $HOME WITH its own .ccb/ should resolve to itself,
    not to $HOME's .ccb/."""
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    (fake_home / ".ccb").mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Project with its own .ccb/
    proj = fake_home / "myproject"
    proj.mkdir()
    (proj / ".ccb").mkdir()
    subdir = proj / "src" / "lib"
    subdir.mkdir(parents=True)

    # Should resolve to proj, not fake_home
    assert find_project_root(subdir) == proj
    assert compute_ccb_project_id(subdir) == compute_ccb_project_id(proj)


def test_nested_ccb_stops_at_nearest_anchor(tmp_path: Path) -> None:
    """When a subdirectory has its own .ccb/, it should be its own project root."""
    (tmp_path / ".ccb").mkdir()
    inner = tmp_path / "sub_project"
    inner.mkdir()
    (inner / ".ccb").mkdir()
    deep = inner / "src"
    deep.mkdir()

    # inner and deep should resolve to inner, not tmp_path
    assert find_project_root(inner) == inner
    assert find_project_root(deep) == inner
    assert find_project_root(tmp_path) == tmp_path

    # Project IDs should differ between outer and inner projects
    assert compute_ccb_project_id(tmp_path) != compute_ccb_project_id(inner)
    # But inner and its subdirectory should match
    assert compute_ccb_project_id(inner) == compute_ccb_project_id(deep)
