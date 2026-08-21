"""Refuse to run test helpers against the repo's live data/ directory.

tests/conftest.py binds SORTIFY_DATA_DIR to a throwaway dir — but only
pytest loads conftest. Any `python -c` snippet, REPL, or directly-executed
test module that imports sortify.app binds Store to the real data/, and on
2026-08-21 exactly that overwrote the live config.json and folders.json
with fixture data. Every appmod-importing test module calls this at import
time so that mistake dies loudly instead of writing live files.

Identity check, not a naming convention: only the repo's own data/ is
refused, so tmp_path Stores and the fuzz harness's sortify-fuzz-* dirs
pass untouched.
"""

from pathlib import Path

LIVE_DATA_DIR = (Path(__file__).resolve().parent.parent / "data").resolve()


def assert_not_live_data(store_dir) -> None:
    if Path(store_dir).resolve() == LIVE_DATA_DIR:
        raise RuntimeError(
            f"refusing: Store is bound to the live data directory ({LIVE_DATA_DIR}). "
            "Run under pytest (tests/conftest.py binds SORTIFY_DATA_DIR to a temp "
            "dir), or export SORTIFY_DATA_DIR to a scratch dir before importing "
            "sortify.app. Writing here overwrites the real config/cache."
        )
