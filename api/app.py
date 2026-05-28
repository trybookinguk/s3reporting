"""
Dashboard API — reads the SQLite warehouse, returns the same JSON shapes the
Svelte dashboard currently consumes from SharePoint.

Runs locally on the Pi (127.0.0.1:8000) and is reverse-proxied by the Svelte
app at :3000. Each endpoint runs SQL against ``warehouse.db`` on demand — no
caching layer; SQLite is fast enough on indexed aggregates and the warehouse
is updated daily by ``prepare_data.py``.

Start with::

    uvicorn api.app:app --host 127.0.0.1 --port 8000

Or via the systemd unit at ``deploy/dashboard-api.service``.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from fastapi import FastAPI

from modules import warehouse


app = FastAPI(title="Dashboard API", version="1.0.0")


def _db_path() -> str:
    """Resolve the warehouse path the same way the warehouse module does."""
    return os.environ.get("WAREHOUSE_DB") or warehouse.default_db_path()


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Per-request connection. SQLite handles its own locking; WAL mode (set
    when the warehouse was created) lets reads run alongside the daily upsert.
    """
    conn = sqlite3.connect(_db_path(), timeout=30, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


@app.get("/health")
def health() -> dict:
    """Liveness probe — returns warehouse row counts and last ingest time."""
    with db() as conn:
        return warehouse.summary(conn)


# Endpoints get registered here as we migrate. Each lives in its own module
# under api/endpoints/ so they're easy to find and test in isolation.
from api.endpoints import daily_metrics  # noqa: E402

app.include_router(daily_metrics.router)
