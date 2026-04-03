"""SQLite database setup with schema initialisation.

Uses WAL mode for concurrent read/write from the async drain loop
and any read-only CLI queries (``main.py stats``).

**Litestream compatibility**: auto-checkpointing is disabled so that
Litestream's own checkpoint management is not conflicted.  A generous
``busy_timeout`` avoids ``SQLITE_BUSY`` when Litestream holds a read
lock during replication.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..logging_config import get_logger

logger = get_logger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    order_id        TEXT PRIMARY KEY,
    strategy        TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    slug            TEXT NOT NULL,
    side            TEXT NOT NULL,
    price           REAL NOT NULL,
    size            REAL NOT NULL,
    initial_status  TEXT NOT NULL,
    final_status    TEXT,
    rejection_reason TEXT DEFAULT '',
    placed_at       REAL NOT NULL,
    resolved_at     REAL,
    signal_to_rest_ms REAL,
    signal_to_fill_ms REAL,
    tick_to_order_ms REAL,
    time_to_expiry_s REAL,
    market          TEXT DEFAULT '',
    best_bid        REAL,
    best_ask        REAL,
    spot_price       REAL,
    strike_price     REAL,
    proximity        REAL,
    spot_price_age_ms REAL,
    submission_source TEXT DEFAULT 'unknown',
    sign_ms         REAL,
    post_ms         REAL,
    dry_run         INTEGER NOT NULL DEFAULT 0,
    tag             TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        TEXT NOT NULL,
    fill_price      REAL NOT NULL,
    fill_size       REAL NOT NULL,
    cumulative_filled REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'ws',
    timestamp       REAL NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id)
);

CREATE TABLE IF NOT EXISTS positions (
    token_id        TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    slug            TEXT NOT NULL,
    quantity        REAL NOT NULL DEFAULT 0,
    avg_entry_price REAL NOT NULL DEFAULT 0,
    realized_pnl    REAL NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (token_id, strategy)
);

CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL NOT NULL,
    strategy        TEXT NOT NULL,
    slug            TEXT NOT NULL,
    trigger         TEXT NOT NULL,
    decision        TEXT NOT NULL,
    reason          TEXT DEFAULT '',
    best_outcome    TEXT DEFAULT '',
    best_price      REAL,
    threshold       REAL,
    limit_price     REAL,
    order_id        TEXT DEFAULT '',
    price_source    TEXT DEFAULT '',
    raw_prices      TEXT DEFAULT '',
    dry_run         INTEGER NOT NULL DEFAULT 0,
    tag             TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id        TEXT PRIMARY KEY,
    strategy        TEXT NOT NULL,
    slug            TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    exit_price      REAL,
    size            REAL NOT NULL,
    gross_pnl       REAL DEFAULT 0,
    net_pnl         REAL DEFAULT 0,
    fees            REAL DEFAULT 0,
    hold_duration_s REAL DEFAULT 0,
    timestamp_entry REAL NOT NULL,
    timestamp_exit  REAL,
    spot_price       REAL,
    dry_run         INTEGER NOT NULL DEFAULT 0,
    tag             TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dedup (
    slug            TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    session_date    TEXT NOT NULL,
    created_at      REAL NOT NULL,
    PRIMARY KEY (slug, token_id, strategy, session_date)
);

CREATE TABLE IF NOT EXISTS metrics_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL NOT NULL,
    snapshot_json   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_slug ON orders(slug);
CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders(strategy);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);
CREATE INDEX IF NOT EXISTS idx_decisions_slug ON decisions(slug);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_dedup_session ON dedup(session_date);
"""

_POST_MIGRATION_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_orders_tag ON orders(tag)",
    "CREATE INDEX IF NOT EXISTS idx_trades_tag ON trades(tag)",
]


_MIGRATIONS = [
    ("orders", "market", "ALTER TABLE orders ADD COLUMN market TEXT DEFAULT ''"),
    ("orders", "best_bid", "ALTER TABLE orders ADD COLUMN best_bid REAL"),
    ("orders", "best_ask", "ALTER TABLE orders ADD COLUMN best_ask REAL"),
    ("fills", "cumulative_filled", "ALTER TABLE fills ADD COLUMN cumulative_filled REAL NOT NULL DEFAULT 0"),
    ("fills", "source", "ALTER TABLE fills ADD COLUMN source TEXT NOT NULL DEFAULT 'ws'"),
    ("orders", "underlying_price", "ALTER TABLE orders ADD COLUMN underlying_price REAL"),
    ("trades", "underlying_price", "ALTER TABLE trades ADD COLUMN underlying_price REAL"),
    ("orders", "spot_price", "ALTER TABLE orders ADD COLUMN spot_price REAL"),
    ("orders", "strike_price", "ALTER TABLE orders ADD COLUMN strike_price REAL"),
    ("orders", "proximity", "ALTER TABLE orders ADD COLUMN proximity REAL"),
    ("orders", "spot_price_age_ms", "ALTER TABLE orders ADD COLUMN spot_price_age_ms REAL"),
    ("orders", "submission_source", "ALTER TABLE orders ADD COLUMN submission_source TEXT DEFAULT 'unknown'"),
    ("trades", "spot_price", "ALTER TABLE trades ADD COLUMN spot_price REAL"),
    ("orders", "sign_ms", "ALTER TABLE orders ADD COLUMN sign_ms REAL"),
    ("orders", "post_ms", "ALTER TABLE orders ADD COLUMN post_ms REAL"),
    ("orders", "tag", "ALTER TABLE orders ADD COLUMN tag TEXT DEFAULT ''"),
    ("trades", "tag", "ALTER TABLE trades ADD COLUMN tag TEXT DEFAULT ''"),
    ("decisions", "tag", "ALTER TABLE decisions ADD COLUMN tag TEXT DEFAULT ''"),
]


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Add columns that may be missing from an older schema.

    Each migration is applied and committed independently so that a
    failure in one does not block subsequent migrations.
    """
    for table, column, sql in _MIGRATIONS:
        try:
            cursor = conn.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in cursor.fetchall()}
            if column not in existing:
                conn.execute("BEGIN")
                conn.execute(sql)
                conn.execute("COMMIT")
                logger.info("[DB] Migration: added %s.%s", table, column)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            logger.exception(
                "[DB] Migration FAILED for %s.%s — will retry next startup",
                table,
                column,
            )


def init_db(db_path: str) -> sqlite3.Connection:
    """Create the database file (if needed) and ensure all tables exist.

    Returns a connection with WAL mode and foreign-key enforcement enabled.

    Key settings for Litestream compatibility:

    * ``wal_autocheckpoint=0`` — lets Litestream manage checkpointing;
      concurrent auto-checkpoints by SQLite and Litestream are the #1
      cause of WAL corruption.
    * ``busy_timeout=5000`` — waits up to 5 s instead of failing
      immediately when Litestream (or a read-only connection) holds a
      shared lock.
    * ``synchronous=NORMAL`` — safe for WAL mode; guarantees durability
      at the WAL level while avoiding the fsync-per-commit overhead of
      FULL.
    * ``isolation_level=None`` — disables Python's implicit transaction
      management so that the persistence layer can issue explicit
      ``BEGIN`` / ``COMMIT`` without conflicting with the module's
      internal state.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(path),
        check_same_thread=False,
        isolation_level=None,
    )

    journal_mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if journal_mode.lower() != "wal":
        logger.warning(
            "[DB] journal_mode returned '%s' instead of 'wal' — "
            "Litestream replication may not work correctly",
            journal_mode,
        )

    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA wal_autocheckpoint=0")

    conn.execute("BEGIN")
    for statement in SCHEMA_SQL.split(";"):
        statement = statement.strip()
        if statement:
            conn.execute(statement)
    conn.execute("COMMIT")

    _run_migrations(conn)

    conn.execute("BEGIN")
    for idx_sql in _POST_MIGRATION_INDEXES:
        conn.execute(idx_sql)
    conn.execute("COMMIT")

    logger.info("[DB] Initialised database at %s (journal_mode=%s)", db_path, journal_mode)
    return conn


def get_readonly_connection(db_path: str) -> sqlite3.Connection:
    """Open a read-only connection for CLI stat queries."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn
