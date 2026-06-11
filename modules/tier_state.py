"""
SQLite-backed store for tier pipeline state (tier_history + tier_snapshot).

These used to be single JSON files on SharePoint that zoho_tiers.py read at the
start of each run and wrote back at the end. They are read-write PIPELINE STATE
(not dashboard data), so they move to a dedicated writable SQLite DB on the Pi —
the same place the dashboard's other writable stores live, and now covered by
backup_to_sharepoint.py (CRITICAL_FILES).

One row per logical file, value = the JSON blob, round-tripped verbatim. This
mirrors the previous whole-file read/rewrite access pattern exactly (the history
blob can be tens of MB; it's loaded and rewritten wholesale each run either way).
"""

import json
import logging
import os
import sqlite3

log = logging.getLogger(__name__)

TIER_STATE_DB = os.environ.get(
    "TIER_STATE_DB", "/root/s3reporting/.cache/prepared/tier_state.db"
)


def _connect(read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        return sqlite3.connect(f"file:{TIER_STATE_DB}?mode=ro", uri=True, timeout=30)
    conn = sqlite3.connect(TIER_STATE_DB, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tier_state ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    return conn


def read_blob(key: str):
    """Return the parsed JSON value stored under `key`, or None if absent."""
    if not os.path.exists(TIER_STATE_DB):
        return None
    try:
        conn = _connect(read_only=True)
        try:
            row = conn.execute(
                "SELECT value FROM tier_state WHERE key = ?", (key,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.error("tier_state read of %s failed: %s", key, e)
        return None
    if not row:
        return None
    return json.loads(row[0])


def write_blob(key: str, value) -> bool:
    """Upsert a JSON-serialisable value under `key`. Returns True on success."""
    from datetime import datetime, timezone
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO tier_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.error("tier_state write of %s failed: %s", key, e)
        return False
    return True
