"""Bind the data directory before anything imports sortify.app.

app.py builds its Store and Spotify client at module scope, so a bare import
would otherwise bind to the real data/ — reading tokens.json and, worse,
writing the live budget ledger. Tests get a throwaway directory instead.
"""

import os
import tempfile

os.environ["SORTIFY_DATA_DIR"] = tempfile.mkdtemp(prefix="sortify-tests-")
