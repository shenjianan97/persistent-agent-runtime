"""Real-provider offline evaluation suite.

Excluded from default ``pytest`` collection via ``pyproject.toml``'s
``testpaths`` + ``norecursedirs``. Invoke explicitly:

    services/worker-service/.venv/bin/pytest tests_offline/ -m offline

See ``README.md`` for the full runbook.
"""
