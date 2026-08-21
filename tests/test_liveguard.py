"""The guard that keeps test code off the real data/ directory.

2026-08-21: app-level test helpers, run outside pytest (where conftest's
SORTIFY_DATA_DIR binding does not exist), overwrote the live config.json and
folders.json with fixture data. The guard refuses at import time instead of
writing live files. It checks identity with the repo's data/ dir rather than
requiring a temp-dir naming prefix, so legitimate tmp_path Stores and the
fuzz harness's own binding stay untouched.
"""

from pathlib import Path

import pytest

from liveguard import LIVE_DATA_DIR, assert_not_live_data


def test_the_repo_data_dir_is_refused():
    with pytest.raises(RuntimeError, match="live data"):
        assert_not_live_data(LIVE_DATA_DIR)


def test_the_repo_data_dir_is_refused_via_relative_path():
    rel = Path(__file__).parent.parent / "sortify" / ".." / "data"
    with pytest.raises(RuntimeError):
        assert_not_live_data(rel)


def test_temp_dirs_pass(tmp_path):
    assert_not_live_data(tmp_path)  # must not raise


def test_the_suite_itself_is_isolated():
    """The guard guarding the guard: this very pytest run must be bound to
    a throwaway dir, or every appmod-importing module below would refuse."""
    from sortify import app as appmod

    assert_not_live_data(appmod.store.dir)
