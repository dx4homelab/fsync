"""Shared test guards.

Auto-apply streak tracking (ISSUE-006 fix B) persists under ``state_root()``;
no test may touch the real ``~/.local/state/fsync``."""

import pytest

from fsync import homesync as hs


@pytest.fixture(autouse=True)
def _isolated_state_root(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "STATE_ROOT", str(tmp_path / "state"))
