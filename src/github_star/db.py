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

-- M2: converged category table (20–40 rows, the milestone's core output)
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    member_tags TEXT,                          -- JSON array of merged free tags
    created_at  TEXT
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


# --- M2: tagging pipeline helpers -------------------------------------------

def repos_missing_readme(conn: sqlite3.Connection,
                         retag_stale: bool = False, ttl_days: int = 90):
    """Repos whose README still needs fetching.

    Always includes rows with a NULL `readme`. When `retag_stale`, also
    re-fetches READMEs for repos whose analysis is stale/absent, so a periodic
    refresh picks up README changes too.
    """
    if retag_stale:
        return conn.execute(
            """
            SELECT r.* FROM repos r
            LEFT JOIN repo_analysis a ON a.repo_id = r.id
            WHERE r.readme IS NULL
               OR a.analyzed_at IS NULL
               OR julianday('now') - julianday(a.analyzed_at) > ?
            """,
            (ttl_days,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM repos WHERE readme IS NULL"
    ).fetchall()


def repos_to_tag(conn: sqlite3.Connection,
                 retag_stale: bool = False, ttl_days: int = 90,
                 limit: int | None = None):
    """Repos that need round-1 tagging.

    A repo is due when it has no round-1 tags yet, OR (`retag_stale`) its
    analysis is stale by `analyzed_at` — the single expiry gate (§澄清①).
    Already-tagged, non-stale repos are excluded → they never reach the LLM.
    """
    sql = """
        SELECT r.* FROM repos r
        LEFT JOIN repo_analysis a ON a.repo_id = r.id
        WHERE NOT EXISTS (
            SELECT 1 FROM repo_tags t WHERE t.repo_id = r.id AND t.round = 1
        )
    """
    params: list = []
    if retag_stale:
        sql += (" OR a.analyzed_at IS NULL"
                " OR julianday('now') - julianday(a.analyzed_at) > ?")
        params.append(ttl_days)
    sql += " ORDER BY r.stargazers_count DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def set_readme(conn: sqlite3.Connection, repo_id: int, readme: str | None) -> None:
    conn.execute("UPDATE repos SET readme = ? WHERE id = ?", (readme, repo_id))


def clear_round1_tags(conn: sqlite3.Connection, repo_id: int) -> None:
    """Drop a repo's round-1 tags before a re-tag (avoids stale-tag residue)."""
    conn.execute("DELETE FROM repo_tags WHERE repo_id = ? AND round = 1",
                 (repo_id,))


def add_round1_tags(conn: sqlite3.Connection, repo_id: int, tags: list[str],
                    model: str, created_at: str) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO repo_tags(repo_id, tag, round, model, created_at) "
        "VALUES (?, ?, 1, ?, ?)",
        [(repo_id, t, model, created_at) for t in tags],
    )


def upsert_analysis(conn: sqlite3.Connection, repo_id: int, fields: dict) -> None:
    """Insert-or-update a repo_analysis row, patching only the given columns."""
    cols = ("category", "audience", "niche_bucket", "status_class",
            "summary", "model", "analyzed_at")
    present = {k: v for k, v in fields.items() if k in cols}
    if not present:
        return
    conn.execute(
        "INSERT INTO repo_analysis(repo_id) VALUES (?) "
        "ON CONFLICT(repo_id) DO NOTHING",
        (repo_id,),
    )
    assignments = ", ".join(f"{k} = ?" for k in present)
    conn.execute(
        f"UPDATE repo_analysis SET {assignments} WHERE repo_id = ?",
        (*present.values(), repo_id),
    )


def all_round1_tags(conn: sqlite3.Connection) -> list[str]:
    """Distinct round-1 tags across all repos (input to clustering)."""
    return [r["tag"] for r in conn.execute(
        "SELECT DISTINCT tag FROM repo_tags WHERE round = 1 ORDER BY tag"
    ).fetchall()]


def round1_tag_frequency(conn: sqlite3.Connection) -> list[tuple]:
    """(tag, count) pairs for round-1 tags, most frequent first."""
    return [(r["tag"], r["n"]) for r in conn.execute(
        "SELECT tag, COUNT(*) AS n FROM repo_tags WHERE round = 1 "
        "GROUP BY tag ORDER BY n DESC, tag"
    ).fetchall()]


def replace_categories(conn: sqlite3.Connection, cats: list[dict],
                       created_at: str) -> None:
    """Overwrite the categories table with a freshly converged set."""
    import json
    conn.execute("DELETE FROM categories")
    conn.executemany(
        "INSERT INTO categories(name, description, member_tags, created_at) "
        "VALUES (?, ?, ?, ?)",
        [(c["name"], c.get("description"),
          json.dumps(c.get("member_tags") or [], ensure_ascii=False),
          created_at) for c in cats],
    )


def category_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM categories").fetchone()["c"]


def category_names(conn: sqlite3.Connection) -> list[str]:
    return [r["name"] for r in conn.execute(
        "SELECT name FROM categories ORDER BY name").fetchall()]


def repos_needing_backfill(conn: sqlite3.Connection):
    """Repos with round-1 tags but no category assigned yet (resumable)."""
    return conn.execute(
        """
        SELECT r.* FROM repos r
        WHERE EXISTS (
            SELECT 1 FROM repo_tags t WHERE t.repo_id = r.id AND t.round = 1
        ) AND NOT EXISTS (
            SELECT 1 FROM repo_analysis a
            WHERE a.repo_id = r.id AND a.category IS NOT NULL
        )
        """
    ).fetchall()


def round1_tags_for(conn: sqlite3.Connection, repo_id: int) -> list[str]:
    return [r["tag"] for r in conn.execute(
        "SELECT tag FROM repo_tags WHERE repo_id = ? AND round = 1 ORDER BY tag",
        (repo_id,),
    ).fetchall()]
