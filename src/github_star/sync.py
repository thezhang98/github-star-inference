"""sync_stars business logic: pull starred repos into SQLite, incrementally."""
import json
import sqlite3
from datetime import datetime, timezone

from . import db
from .github import GitHubClient

_INSERT = """
INSERT OR IGNORE INTO repos (
    id, full_name, name, owner, description, language, topics,
    stargazers_count, forks_count, open_issues_count, html_url, homepage,
    license, is_fork, is_archived, is_disabled, default_branch,
    repo_created_at, repo_updated_at, pushed_at, starred_at, readme, synced_at
) VALUES (
    :id, :full_name, :name, :owner, :description, :language, :topics,
    :stargazers_count, :forks_count, :open_issues_count, :html_url, :homepage,
    :license, :is_fork, :is_archived, :is_disabled, :default_branch,
    :repo_created_at, :repo_updated_at, :pushed_at, :starred_at, :readme,
    :synced_at
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(item: dict, synced_at: str) -> dict:
    """Flatten a {starred_at, repo} item into a repos row (readme left empty)."""
    repo = item["repo"]
    return {
        "id": repo["id"],
        "full_name": repo["full_name"],
        "name": repo.get("name"),
        "owner": (repo.get("owner") or {}).get("login"),
        "description": repo.get("description"),
        "language": repo.get("language"),
        "topics": json.dumps(repo.get("topics") or []),
        "stargazers_count": repo.get("stargazers_count"),
        "forks_count": repo.get("forks_count"),
        "open_issues_count": repo.get("open_issues_count"),
        "html_url": repo.get("html_url"),
        "homepage": repo.get("homepage"),
        "license": (repo.get("license") or {}).get("spdx_id"),
        "is_fork": 1 if repo.get("fork") else 0,
        "is_archived": 1 if repo.get("archived") else 0,
        "is_disabled": 1 if repo.get("disabled") else 0,
        "default_branch": repo.get("default_branch"),
        "repo_created_at": repo.get("created_at"),
        "repo_updated_at": repo.get("updated_at"),
        "pushed_at": repo.get("pushed_at"),
        "starred_at": item["starred_at"],
        "readme": None,  # M2 fills this
        "synced_at": synced_at,
    }


def sync_stars(conn: sqlite3.Connection, client: GitHubClient,
               full: bool = False) -> dict:
    """Pull starred repos into `repos`, deduping by GitHub repo id.

    Incremental mode (DB non-empty and last sync completed): stop early when a
    whole page is already known — because results are starred_at-desc, the DB
    holds the newest prefix. First-run/resume mode (DB empty or last sync
    interrupted): walk to the last page; INSERT OR IGNORE keeps reruns
    idempotent. full=True forces a complete walk with no early stop.
    """
    started_at = _now()
    prior_count = db.repo_count(conn)
    last_status = db.get_meta(conn, "last_sync_status")
    incremental = (not full) and prior_count > 0 and last_status == "completed"

    db.set_meta(conn, "last_sync_status", "in_progress")
    db.set_meta(conn, "last_sync_started_at", started_at)
    conn.commit()

    inserted = 0
    for page in client.iter_starred():
        rows = [_row(item, started_at) for item in page]
        cur = conn.executemany(_INSERT, rows)
        page_inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        inserted += page_inserted
        conn.commit()  # per-page commit → interrupt-safe / resumable

        # incremental short-circuit: an entire page already known means the
        # rest (older stars) is already synced.
        if incremental and page_inserted == 0:
            break

    completed_at = _now()
    db.set_meta(conn, "last_sync_status", "completed")
    db.set_meta(conn, "last_sync_completed_at", completed_at)
    db.set_meta(conn, "last_sync_count", inserted)
    conn.commit()

    return {
        "synced": inserted,
        "total_in_db": db.repo_count(conn),
        "mode": "incremental" if incremental else "full",
        "started_at": started_at,
        "completed_at": completed_at,
    }
