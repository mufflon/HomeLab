"""The run lock: never two paperbridge runs at once."""

import pytest

from paperbridge import __main__ as cli
from paperbridge.config import REQUIRED


def test_a_second_lock_is_refused_until_the_first_is_released(tmp_path):
    state = str(tmp_path / "state.json")
    first = cli.run_lock(state)
    assert first is not None
    assert cli.run_lock(state) is None
    first.close()
    again = cli.run_lock(state)
    assert again is not None
    again.close()


def test_no_state_path_means_no_lock():
    with cli.run_lock(None):
        with cli.run_lock(None):
            pass


def test_a_run_while_another_holds_the_lock_does_nothing(tmp_path, monkeypatch):
    for key in REQUIRED:
        monkeypatch.setenv(key, "x")
    state = str(tmp_path / "state.json")
    monkeypatch.setenv("STATE_PATH", state)

    def handoff(*args, **kwargs):
        raise AssertionError("the second run started a handoff")

    monkeypatch.setattr(cli, "Handoff", handoff)
    held = cli.run_lock(state)
    try:
        assert cli.main([]) == 0
    finally:
        held.close()
    with pytest.raises(AssertionError, match="started a handoff"):
        cli.main([])
