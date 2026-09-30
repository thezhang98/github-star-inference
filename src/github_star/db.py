"""SQLite connection and schema management."""
import sqlite3

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id                INTEGER PRIMARY KEY,   -- GitHub repo id (dedup key)
    full_name         TEXT NOT NULL,
    name              TEXT,
    owner             TEXT,
    description       TEXT,
    language          TEXT,
    topics            TEXT,                  -- JSON array
    stargazers_count  INTEGER,
    forks_count       INTEGER,
    open_issues_count INTEGER,
    html_url          TEXT,
    homepage          TEXT,
    license           TEXT,                  -- license.spdx_id
    is_fork           INTEGER,
    is_archived       INTEGER,
    is_disabled       INTEGER,
    default_branch    TEXT,
    repo_created_at   TEXT,
    repo_updated_at   TEXT,
    pushed_at         TEXT,
    starred_at        TEXT NOT NULL,
    readme            TEXT,                  -- reserved for M2, left empty in M1
    synced_at         TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_repos_full_name ON repos(full_name);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- M2 placeholder tables (created in M1, not written to)
CREATE TABLE IF NOT EXISTS repo_tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id    INTEGER NOT NULL REFERENCES repos(id),
    tag        TEXT NOT NULL,
    round      INTEGER NOT NULL,             -- 1=free tagging / 2=backfill
    model      TEXT,
    created_at TEXT,
    UNIQUE(repo_id, tag, round)
);

CREATE TABLE IF NOT EXISTS repo_analysis (
    repo_id      INTEGER PRIMARY KEY REFERENCES repos(id),
    category     TEXT,
    audience     TEXT,
    niche_bucket TEXT,
    status_class TEXT,
    summary      TEXT,
    model        TEXT,
    analyzed_at  TEXT
);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a connection, initialize schema, and record schema_version."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def repo_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM repos").fetchone()["c"]
