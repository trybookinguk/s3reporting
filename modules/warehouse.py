#!/usr/bin/env python3
"""
Local SQLite warehouse.

Accumulates the S3 reports into a single SQLite database on the Pi so jobs can
query locally instead of re-downloading and re-combining from S3 each run.

Tables
------
- ``bookings``  : transaction log, keyed by BookingTransactionId. Maintained by
                  upsert (INSERT OR REPLACE) so the newest version of a row
                  wins — transactions whose Status/fees are revised after first
                  appearing are corrected in place. Seeded once from
                  BookingDataAll, then kept current by the daily BookingData
                  (current-month-to-date) file.
- ``accounts``  : current account snapshot, full-replaced each run.
- ``users``     : current user snapshot, full-replaced each run.
- ``meta``      : key/value bookkeeping (last run, row counts, schema version).

Design notes
------------
- Rows are never deleted on absence. The daily BookingData only covers the
  current month, so prior-month rows are simply not in it — they already live
  in the table and must stay.
- Dates are stored as ISO-8601 strings (SQLite has no native datetime). The
  ``read_*`` helpers re-parse them so callers get the same dtypes the pickle
  path produces.
- Each ingest runs inside a single transaction, so a crash mid-run leaves the
  table at its previous consistent state rather than half-applied.
"""

import logging
import os
import sqlite3
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
BOOKINGS_KEY = "BookingTransactionId"


def default_db_path() -> str:
    """Resolve the warehouse path: WAREHOUSE_DB, else DATA_DIR/warehouse.db."""
    explicit = os.environ.get("WAREHOUSE_DB")
    if explicit:
        return explicit
    data_dir = os.environ.get("DATA_DIR", os.path.join(os.environ.get("S3_CACHE_DIR", ".cache"), "prepared"))
    return os.path.join(data_dir, "warehouse.db")


def connect(db_path: str = None) -> sqlite3.Connection:
    """Open (creating if needed) the warehouse with sensible pragmas."""
    path = db_path or default_db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    # WAL lets a reader (a query job) run while prepare_data writes; NORMAL sync
    # is durable enough for a rebuildable cache and much faster than FULL.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    try:
        cur = conn.execute("SELECT value FROM meta WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else default
    except sqlite3.OperationalError:
        return default


def _table_rowcount(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


# Columns that are logically integer IDs. Coerced to nullable Int64 before
# write so SQLite stores them as INTEGER, not TEXT like '4349.0' — the latter
# forces every JOIN/lookup query to CAST and bypass the index. Read-side queries
# can then use `AccountId = ?` with an int param and hit the index directly.
_INT_ID_COLUMNS = (
    "BookingTransactionId", "AccountId", "EventId", "CustomerId",
    "DonationCampaignId", "BookingId", "BookingUrlId", "GiftCertificateId",
    # Snapshot tables (accounts.Id, users.Id) also benefit — joins to bookings
    # become INTEGER = INTEGER and the PRIMARY KEY's autoindex applies.
    "Id",
)


def _prepare_for_sqlite(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce dtypes for SQLite storage:
       - datetime → ISO-8601 string (SQLite has no native datetime)
       - category → plain string (categories don't survive to_sql cleanly)
       - ID columns → nullable Int64 (so SQLite stores INTEGER, not '4349.0')
    """
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].astype("string")  # NaT -> <NA> -> NULL
        elif isinstance(out[col].dtype, pd.CategoricalDtype):
            out[col] = out[col].astype("string")
        elif col in _INT_ID_COLUMNS:
            # to_numeric handles strings like '4349.0', floats, and ints alike.
            # Int64 (nullable) preserves NaN as <NA>, which to_sql writes as NULL.
            out[col] = pd.to_numeric(out[col], errors="coerce").astype("Int64")
    return out


# Backwards-compat alias — callers outside this module may still import it.
_stringify_datetimes = _prepare_for_sqlite


def upsert_bookings(conn: sqlite3.Connection, df: pd.DataFrame) -> dict:
    """Insert-or-replace booking rows keyed by BookingTransactionId.

    Returns a small stats dict. The whole operation is one transaction.
    """
    if BOOKINGS_KEY not in df.columns:
        raise ValueError(f"bookings frame missing {BOOKINGS_KEY!r}")

    df = df.dropna(subset=[BOOKINGS_KEY])
    df = _stringify_datetimes(df)

    before = _table_rowcount(conn, "bookings")
    table_exists = before > 0 or _table_exists(conn, "bookings")

    with conn:  # transaction
        if not table_exists:
            # First load (the BookingDataAll seed). Let pandas create the table
            # from the frame, then promote the key to PRIMARY KEY via a rebuild
            # — pandas' to_sql can't declare a PK directly.
            df.to_sql("bookings", conn, if_exists="replace", index=False)
            _add_primary_key(conn, "bookings", BOOKINGS_KEY)
            conn.execute(f"CREATE INDEX IF NOT EXISTS ix_bookings_account ON bookings(AccountId)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS ix_bookings_txndate ON bookings(TransactionDate)")
            inserted = len(df)
            replaced = 0
        else:
            # Upsert into a staging table then INSERT OR REPLACE, so the
            # operation tolerates new columns appearing in the source file.
            _ensure_columns(conn, "bookings", df.columns)
            df.to_sql("_stage_bookings", conn, if_exists="replace", index=False)
            cols = [c for c in df.columns]
            collist = ",".join(f'"{c}"' for c in cols)
            # Count how many of the staged IDs already exist (for stats).
            replaced = conn.execute(
                "SELECT COUNT(*) FROM _stage_bookings s "
                "WHERE EXISTS (SELECT 1 FROM bookings b WHERE b.{k}=s.{k})".format(k=BOOKINGS_KEY)
            ).fetchone()[0]
            conn.execute(
                f"INSERT OR REPLACE INTO bookings ({collist}) "
                f"SELECT {collist} FROM _stage_bookings"
            )
            conn.execute("DROP TABLE _stage_bookings")
            inserted = len(df) - replaced

        _set_meta(conn, "schema_version", SCHEMA_VERSION)
        _set_meta(conn, "bookings_last_ingest", datetime.utcnow().isoformat())
        after = _table_rowcount(conn, "bookings")
        _set_meta(conn, "bookings_rowcount", after)

    stats = {"staged": len(df), "inserted": inserted, "replaced": replaced,
             "rows_before": before, "rows_after": after}
    logger.info("bookings upsert: staged=%d inserted=%d replaced=%d total=%d",
                stats["staged"], inserted, replaced, after)
    return stats


def upsert_bookings_chunks(conn: sqlite3.Connection, chunk_iter,
                           seed: bool = False) -> dict:
    """Stream booking chunks into the table without ever holding the full frame.

    This is the memory-safe ingest path for the Pi: BookingDataAll is ~4.6M
    rows / ~2 GB as a single frame, which OOMs a 4 GB Pi. Each chunk
    (~100k rows) is written with INSERT OR REPLACE and then released, so peak
    memory stays at one chunk.

    `seed=True` is a hint that this is the first bulk load (an empty table);
    the first chunk creates the table + PK + indexes, subsequent chunks upsert.
    """
    staged = inserted = replaced = 0
    rows_before = _table_rowcount(conn, "bookings")
    created = _table_exists(conn, "bookings") and rows_before > 0

    with conn:  # single transaction for the whole stream
        for chunk in chunk_iter:
            if chunk is None or chunk.empty:
                continue
            if BOOKINGS_KEY not in chunk.columns:
                raise ValueError(f"bookings chunk missing {BOOKINGS_KEY!r}")
            chunk = chunk.dropna(subset=[BOOKINGS_KEY])
            # The loader types BookingTransactionId as float64 to tolerate NaNs
            # in the raw CSV. With NaNs dropped the values are whole numbers, so
            # cast to a true integer before staging — keeps the key stored as
            # INTEGER (matching the PK) rather than REAL like 1234.0.
            chunk[BOOKINGS_KEY] = chunk[BOOKINGS_KEY].astype("int64")
            # Streamed CSV chunks can contain duplicate BookingTransactionIds
            # within a single chunk (e.g. when the fallback path stitches
            # multi-file source data). Dedupe per-chunk so the table-create's
            # PRIMARY KEY rebuild doesn't blow up, and so the cross-chunk
            # INSERT OR REPLACE has a clean staging table to source from.
            chunk = chunk.drop_duplicates(subset=[BOOKINGS_KEY], keep='last')
            chunk = _stringify_datetimes(chunk)
            staged += len(chunk)

            if not created:
                chunk.to_sql("bookings", conn, if_exists="replace", index=False)
                _add_primary_key(conn, "bookings", BOOKINGS_KEY)
                conn.execute("CREATE INDEX IF NOT EXISTS ix_bookings_account ON bookings(AccountId)")
                conn.execute("CREATE INDEX IF NOT EXISTS ix_bookings_txndate ON bookings(TransactionDate)")
                created = True
                continue

            _ensure_columns(conn, "bookings", chunk.columns)
            chunk.to_sql("_stage_bookings", conn, if_exists="replace", index=False)
            cols = list(chunk.columns)
            collist = ",".join(f'"{c}"' for c in cols)
            # No per-row probe to split inserted vs replaced — the old
            # `SELECT COUNT(*) ... WHERE EXISTS` ran as a correlated subquery
            # that scanned the whole multi-million-row bookings index once per
            # staged row: fine for a small daily delta but a 15-minute CPU peg on
            # a full month's file. INSERT OR REPLACE leaves the rowcount
            # unchanged for a replaced row, so we derive the split from the net
            # table growth over the whole stream after the loop (one COUNT, not
            # two per chunk).
            conn.execute(
                f"INSERT OR REPLACE INTO bookings ({collist}) "
                f"SELECT {collist} FROM _stage_bookings"
            )
            conn.execute("DROP TABLE _stage_bookings")

        _set_meta(conn, "schema_version", SCHEMA_VERSION)
        _set_meta(conn, "bookings_last_ingest", datetime.utcnow().isoformat())
        after = _table_rowcount(conn, "bookings")
        _set_meta(conn, "bookings_rowcount", after)

    # Net new rows = table growth across the stream. On a fresh seed the table
    # was empty so this equals all staged rows; on an upsert the remainder were
    # in-place replacements of existing BookingTransactionIds.
    if created:
        inserted = after - rows_before
        replaced = staged - inserted
    stats = {"staged": staged, "inserted": inserted, "replaced": replaced,
             "rows_after": after}
    logger.info("bookings chunk upsert: staged=%d inserted=%d replaced=%d total=%d",
                staged, inserted, replaced, after)
    return stats


def seed_bookings_from_frame(conn: sqlite3.Connection, df: pd.DataFrame,
                             chunk_size: int = 200000) -> dict:
    """Bulk-seed bookings from a globally-deduped in-memory frame.

    Fast path for the first warehouse build: write the frame to a plain table
    (no PK constraint to fight) in chunks, then add the PK + indexes at the
    end. ~10x faster than the streaming INSERT-OR-REPLACE seed because there
    are no per-row index lookups during the bulk write.

    Caller must guarantee BookingTransactionId is globally unique in `df`
    (the combined_booking.pkl produced by `prepare_data.py --combined` is).
    """
    if BOOKINGS_KEY not in df.columns:
        raise ValueError(f"frame missing {BOOKINGS_KEY!r}")
    df = df.dropna(subset=[BOOKINGS_KEY])
    # Safety: dedupe just in case — cheap on a frame this size.
    before_dedupe = len(df)
    df = df.drop_duplicates(subset=[BOOKINGS_KEY], keep='last')
    if len(df) < before_dedupe:
        logger.warning("Dropped %d duplicate %s in seed frame",
                       before_dedupe - len(df), BOOKINGS_KEY)
    df = _stringify_datetimes(df)

    # Drop any prior bookings table so the seed is unambiguous.
    with conn:
        conn.execute("DROP TABLE IF EXISTS bookings")

    # Bulk-write chunks of the frame. No PK yet — pure INSERTs into a plain
    # table are the fastest path SQLite offers.
    total = len(df)
    written = 0
    for i in range(0, total, chunk_size):
        sub = df.iloc[i:i + chunk_size]
        sub.to_sql("bookings", conn, if_exists="append", index=False)
        written += len(sub)
        if (i // chunk_size) % 5 == 0:
            logger.info("  seed-from-frame: %d / %d rows", written, total)

    # Promote the key to PRIMARY KEY (rebuilds the table once) + indexes.
    with conn:
        _add_primary_key(conn, "bookings", BOOKINGS_KEY)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_bookings_account ON bookings(AccountId)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_bookings_txndate ON bookings(TransactionDate)")
        _set_meta(conn, "schema_version", SCHEMA_VERSION)
        _set_meta(conn, "bookings_last_ingest", datetime.utcnow().isoformat())
        _set_meta(conn, "bookings_rowcount", written)

    logger.info("seed_bookings_from_frame complete: %d rows", written)
    return {"staged": total, "inserted": written, "replaced": 0, "rows_after": written}


def replace_snapshot(conn: sqlite3.Connection, table: str, df: pd.DataFrame,
                     key: str = None) -> int:
    """Full-replace a snapshot table (accounts, users). Returns row count."""
    df = _stringify_datetimes(df)
    with conn:
        df.to_sql(table, conn, if_exists="replace", index=False)
        if key and key in df.columns:
            try:
                _add_primary_key(conn, table, key)
            except Exception as e:
                logger.warning("Could not set PK %s on %s: %s", key, table, e)
        _set_meta(conn, f"{table}_last_ingest", datetime.utcnow().isoformat())
        _set_meta(conn, f"{table}_rowcount", len(df))
    logger.info("%s snapshot replaced: %d rows", table, len(df))
    return len(df)


def write_retention_priority(conn: sqlite3.Connection, updates: pd.DataFrame) -> int:
    """Persist the per-account retention priority into the warehouse.

    The dashboard's /retention worklist surfaces the *exact* priority the
    pandas tier pipeline already computes (retention_priority.py), rather than
    re-deriving it in JS. zoho_tiers.py calls this after process_accounts so
    the figure is the canonical one the CS team trusts.

    ``updates`` is process_accounts' frame: Account_Name holds the AccountId,
    Retention_Priority is the Low/Medium/High/Very High category, and
    _retention_priority_score is the numeric score. We snapshot just those into
    a small ``retention_priority`` table keyed by account_id; prepare_data's
    next DuckDB materialise copies it across as ``retention_agg`` for the
    dashboard to SELECT.

    Note on timing: prepare_data (≈02:00) materialises DuckDB *before*
    zoho_tiers (≈02:45) runs, so the priority the dashboard reads is from the
    previous tier run — about a day old. That is consistent with how the
    dashboard's tier/rating already lag (they too derive from the prior
    pipeline run), so "priority" and "current tier" shown side-by-side stay
    coherent.
    """
    required = {"Account_Name", "Retention_Priority"}
    missing = required - set(updates.columns)
    if missing:
        raise ValueError(f"updates missing columns for retention snapshot: {missing}")

    snap = pd.DataFrame(
        {
            # Account_Name == AccountId (process_accounts sets df['Account_Name']
            # = df.index, the AccountId). Store as string so the DuckDB join to
            # the account_metrics aid (also stringified) lines up.
            "account_id": updates["Account_Name"].astype(str),
            "retention_priority": updates["Retention_Priority"].fillna("").astype(str),
        }
    )
    if "_retention_priority_score" in updates.columns:
        snap["retention_priority_score"] = pd.to_numeric(
            updates["_retention_priority_score"], errors="coerce"
        ).astype("Int64")

    # Drop accounts with no usable id, and de-dupe (keep the highest score) so
    # account_id can be a clean primary key.
    snap = snap[snap["account_id"].notna() & (snap["account_id"] != "")]
    if "retention_priority_score" in snap.columns:
        snap = (
            snap.sort_values("retention_priority_score", na_position="first")
            .drop_duplicates("account_id", keep="last")
        )
    else:
        snap = snap.drop_duplicates("account_id", keep="last")

    return replace_snapshot(conn, "retention_priority", snap, key="account_id")


# ---- read helpers (re-parse dates so callers get pickle-equivalent dtypes) ----

def read_bookings(conn: sqlite3.Connection, where: str = None,
                  params: tuple = ()) -> pd.DataFrame:
    sql = "SELECT * FROM bookings"
    if where:
        sql += f" WHERE {where}"
    df = pd.read_sql_query(sql, conn, params=params)
    return _retype_bookings(df)


def read_table(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    return pd.read_sql_query(f"SELECT * FROM {table}", conn)


def iter_bookings(conn: sqlite3.Connection, where: str = None, params: tuple = (),
                  columns=None, chunk_size: int = 100000):
    """Yield booking rows in chunks — the memory-safe scan primitive.

    Never holds the whole table in memory; each chunk is routed through
    _retype_bookings so dtypes match read_bookings exactly. `columns` selects
    only the columns a caller needs (the aggregator needs ~9 of ~30).

    Filter note: TransactionDate is stored as ISO-8601 UTC strings, so a
    `where` of "TransactionDate >= ?" with a UTC ISO param compares correctly
    (lexicographic order matches chronological order for fixed-format UTC).
    Compute cutoffs as UTC instants, not London date strings.
    """
    collist = "*" if not columns else ",".join(f'"{c}"' for c in columns)
    sql = f"SELECT {collist} FROM bookings"
    if where:
        sql += f" WHERE {where}"
    for chunk in pd.read_sql_query(sql, conn, params=params, chunksize=chunk_size):
        yield _retype_bookings(chunk)


def account_metrics_365(conn: sqlite3.Connection, cutoff_iso: str) -> dict:
    """Per-account aggregate over the last 365 days, for the industry report.

    Returns {account_id: {EventsWithTickets, PaidTicketsIssued, TotalFees}},
    matching the frame-based calculate_account_metrics output. NOTE: does NOT
    filter on Status — the legacy industry-report path sums fees across all
    statuses (Successful + Failed + Unknown), so we must match that to stay
    equivalent. `cutoff_iso` is a UTC ISO timestamp string.
    """
    sql = (
        "SELECT AccountId, "
        "COUNT(DISTINCT EventId) AS EventsWithTickets, "
        "COALESCE(SUM(TicketQuantity), 0) AS PaidTicketsIssued, "
        "ROUND(COALESCE(SUM(BookingFee),0)+COALESCE(SUM(CardFee),0)"
        "+COALESCE(SUM(ProcessingFee),0)+COALESCE(SUM(TicketFee),0), 2) AS TotalFees "
        "FROM bookings WHERE TransactionDate >= ? "
        "GROUP BY AccountId"
    )
    df = pd.read_sql_query(sql, conn, params=(cutoff_iso,))
    out = {}
    for row in df.itertuples(index=False):
        aid = int(row.AccountId) if pd.notna(row.AccountId) else None
        if aid is None:
            continue
        out[aid] = {
            "EventsWithTickets": int(row.EventsWithTickets or 0),
            "PaidTicketsIssued": int(row.PaidTicketsIssued or 0),
            "TotalFees": round(float(row.TotalFees or 0.0), 2),
        }
    return out


def account_last_sale_and_tickets(conn: sqlite3.Connection, cutoff_iso: str,
                                  account_ids=None):
    """For the tier-movement emails: per-account all-time max(TransactionDate)
    and last-365d sum(TicketQuantity), Successful txns only.

    Returns (last_sale: {aid: tz-aware UTC Timestamp}, tickets_365: {aid: int}).
    `account_ids`, if given, scopes both queries to those accounts.
    """
    where = "Status = 'Successful'"
    scope_params = ()
    if account_ids:
        ids = [int(a) for a in account_ids]
        placeholders = ",".join("?" * len(ids))
        where += f" AND AccountId IN ({placeholders})"
        scope_params = tuple(ids)

    last_df = pd.read_sql_query(
        f"SELECT AccountId, MAX(TransactionDate) AS last_sale FROM bookings "
        f"WHERE {where} GROUP BY AccountId",
        conn, params=scope_params,
    )
    last_sale = {}
    for row in last_df.itertuples(index=False):
        if pd.notna(row.AccountId) and row.last_sale is not None:
            last_sale[int(row.AccountId)] = pd.to_datetime(row.last_sale, utc=True)

    tkt_df = pd.read_sql_query(
        f"SELECT AccountId, COALESCE(SUM(TicketQuantity),0) AS tickets FROM bookings "
        f"WHERE {where} AND TransactionDate >= ? GROUP BY AccountId",
        conn, params=scope_params + (cutoff_iso,),
    )
    tickets_365 = {
        int(row.AccountId): int(row.tickets or 0)
        for row in tkt_df.itertuples(index=False) if pd.notna(row.AccountId)
    }
    return last_sale, tickets_365


def read_bookings_grouped(conn: sqlite3.Connection, select_sql: str,
                          where: str = None, params: tuple = (),
                          group_by: str = None) -> pd.DataFrame:
    """Run an aggregate SELECT against bookings and return a small frame.

    `select_sql` is the full SELECT list (e.g.
    "AccountId, SUM(TicketQuantity) AS tickets, COUNT(DISTINCT EventId) AS events").
    Most dashboard builders reduce to one call here. The result is small (one
    row per group), so no chunking is needed.
    """
    sql = f"SELECT {select_sql} FROM bookings"
    if where:
        sql += f" WHERE {where}"
    if group_by:
        sql += f" GROUP BY {group_by}"
    return pd.read_sql_query(sql, conn, params=params)


def _retype_bookings(df: pd.DataFrame) -> pd.DataFrame:
    """Restore the dtypes downstream maths expects after a SQL read."""
    for col in ("TransactionDate", "EventDate"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
    # Key and id columns may come back as object/float depending on storage;
    # restore nullable-int so equality joins against the pickle path match.
    for col in (BOOKINGS_KEY, "AccountId", "EventId"):
        if col in df.columns:
            coerced = pd.to_numeric(df[col], errors="coerce")
            if coerced.notna().any():
                df[col] = coerced.astype("Int64")
    return df


# ---- low-level schema helpers ----

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def _existing_columns(conn: sqlite3.Connection, table: str) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _ensure_columns(conn: sqlite3.Connection, table: str, columns) -> None:
    """Add any source columns not yet present (tolerate new CSV columns)."""
    have = set(_existing_columns(conn, table))
    for col in columns:
        if col not in have:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN "{col}"')
            logger.info("Added new column %r to %s", col, table)


def _add_primary_key(conn: sqlite3.Connection, table: str, key: str) -> None:
    """Rebuild `table` so `key` is the PRIMARY KEY (pandas can't declare one).

    Only called right after a fresh to_sql replace, so the rebuild cost is paid
    once at seed/snapshot time, not on the daily upsert path.
    """
    cols = _existing_columns(conn, table)
    if key not in cols:
        raise ValueError(f"{key!r} not in {table} columns")
    # No explicit type on the key column: forcing TEXT affinity would coerce a
    # numeric BookingTransactionId to a string on storage and read it back as
    # str, diverging from the pickle path (which keeps it numeric). Leaving the
    # type unspecified lets SQLite preserve the value's native storage class.
    col_defs = ", ".join(
        f'"{c}" PRIMARY KEY' if c == key else f'"{c}"' for c in cols
    )
    conn.execute(f"ALTER TABLE {table} RENAME TO _old_{table}")
    conn.execute(f"CREATE TABLE {table} ({col_defs})")
    collist = ",".join(f'"{c}"' for c in cols)
    conn.execute(f"INSERT INTO {table} ({collist}) SELECT {collist} FROM _old_{table}")
    conn.execute(f"DROP TABLE _old_{table}")


def summary(conn: sqlite3.Connection) -> dict:
    """Quick status dict for logging/health checks."""
    return {
        "schema_version": get_meta(conn, "schema_version"),
        "bookings_rows": _table_rowcount(conn, "bookings"),
        "accounts_rows": _table_rowcount(conn, "accounts"),
        "users_rows": _table_rowcount(conn, "users"),
        "bookings_last_ingest": get_meta(conn, "bookings_last_ingest"),
    }
