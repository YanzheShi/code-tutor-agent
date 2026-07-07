"""Per-session generation progress store.

Thread-safe shared dict that both graph and generator nodes write to,
and the API reads from for polling.  Avoids circular imports between
``graph.py`` and ``nodes/generator.py``.
"""

# sid → list of progress message strings
_generation_progress: dict[str, list[str]] = {}