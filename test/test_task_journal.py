from __future__ import annotations

import tempfile

from task_journal import TaskJournal, TaskState


def test_full_lifecycle() -> None:
    with tempfile.TemporaryDirectory() as d:
        journal = TaskJournal(journal_dir=d)
        journal.record("r1", "codex", TaskState.QUEUED)
        journal.record("r1", "codex", TaskState.RUNNING)
        journal.record("r1", "codex", TaskState.COMPLETED)
        assert journal.get_latest_state("r1") == TaskState.COMPLETED
        assert len(journal.get_pending_tasks()) == 0


def test_pending_excludes_terminal() -> None:
    with tempfile.TemporaryDirectory() as d:
        journal = TaskJournal(journal_dir=d)
        journal.record("r1", "codex", TaskState.RUNNING)
        journal.record("r2", "gemini", TaskState.COMPLETED)
        journal.record("r3", "opencode", TaskState.QUEUED)
        assert set(journal.get_pending_tasks().keys()) == {"r1", "r3"}


def test_survives_reopen() -> None:
    with tempfile.TemporaryDirectory() as d:
        TaskJournal(journal_dir=d).record("r1", "codex", TaskState.RUNNING)
        assert TaskJournal(journal_dir=d).get_latest_state("r1") == TaskState.RUNNING


def test_has_timestamp() -> None:
    with tempfile.TemporaryDirectory() as d:
        journal = TaskJournal(journal_dir=d)
        journal.record("r1", "codex", TaskState.QUEUED)
        assert isinstance(journal.read_all()[0]["timestamp"], float)


def test_truncate() -> None:
    with tempfile.TemporaryDirectory() as d:
        journal = TaskJournal(journal_dir=d)
        for i in range(20):
            journal.record(f"r{i}", "codex", TaskState.COMPLETED)
        journal.truncate(keep_last_n=5)
        assert len(journal.read_all()) == 5


def test_fire_and_forget_no_journal_rows() -> None:
    with tempfile.TemporaryDirectory() as d:
        journal = TaskJournal(journal_dir=d)
        assert len(journal.read_all()) == 0
