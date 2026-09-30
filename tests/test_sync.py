"""Offline sync tests using mocked httpx responses (no real API calls).

Covers the offline-verifiable acceptance items from §六:
- dedup idempotency (rerun leaves row count unchanged)
- resume idempotency (interrupted sync completes without duplicates)
- rate-limit branch (mock 429 / reset -> waits then continues, no data lost)
- missing / invalid token error paths (no token; 401 not retried)
"""
import json

import httpx
import pytest

from github_star import config, db
from github_star.github import GitHubClient, GitHubError
from github_star.sync import sync_stars


def _repo(rid: int, name: str) -> dict:
    return {
        "starred_at": f"2024-01-{rid:02d}T00:00:00Z",
        "repo": {
            "id": rid,
            "full_name": f"owner/{name}",
            "name": name,
            "owner": {"login": "owner"},
            "description": "desc",
            "language": "Python",
            "topics": ["a", "b"],
            "stargazers_count": rid,
            "forks_count": 0,
            "open_issues_count": 0,
            "html_url": f"https://github.com/owner/{name}",
            "homepage": None,
            "license": {"spdx_id": "MIT"},
            "fork": False,
            "archived": False,
            "disabled": False,
            "default_branch": "main",
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
            "pushed_at": "2024-01-01T00:00:00Z",
        },
    }


def _page_response(request: httpx.Request, items: list[dict],
                   next_url: str | None, headers: dict | None = None):
    hdrs = {"x-ratelimit-remaining": "5000", "x-ratelimit-reset": "0"}
    if headers:
        hdrs.update(headers)
    if next_url:
        hdrs["Link"] = f'<{next_url}>; rel="next"'
    return httpx.Response(200, json=items, headers=hdrs, request=request)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def _client(handler, sleep_calls=None):
    def _sleep(s):
        if sleep_calls is not None:
            sleep_calls.append(s)
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return GitHubClient("tok", client=http, sleep=_sleep)


def test_schema_and_placeholder_tables(conn):
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"repos", "meta", "repo_tags", "repo_analysis"} <= tables
    assert db.get_meta(conn, "schema_version") == "1"


def test_full_sync_two_pages(conn):
    p2 = "https://api.github.com/user/starred?page=2"

    def handler(request: httpx.Request):
        if request.url.params.get("page") == "1" or "page=2" not in str(request.url):
            return _page_response(request, [_repo(1, "a"), _repo(2, "b")], p2)
        return _page_response(request, [_repo(3, "c")], None)

    result = sync_stars(conn, _client(handler))
    assert result["synced"] == 3
    assert db.repo_count(conn) == 3
    # starred_at populated for every row (proves star+json Accept header path)
    missing = conn.execute(
        "SELECT COUNT(*) FROM repos WHERE starred_at IS NULL").fetchone()[0]
    assert missing == 0
    assert db.get_meta(conn, "last_sync_status") == "completed"


def test_rerun_is_idempotent(conn):
    def handler(request):
        return _page_response(request, [_repo(1, "a"), _repo(2, "b")], None)

    sync_stars(conn, _client(handler))
    r2 = sync_stars(conn, _client(handler))
    assert db.repo_count(conn) == 2
    assert r2["synced"] == 0  # nothing new on rerun


def test_incremental_short_circuit(conn):
    """After a completed sync, a rerun stops at the first fully-known page."""
    page1 = [_repo(2, "b"), _repo(1, "a")]

    def initial(request):
        return _page_response(request, page1, None)

    sync_stars(conn, _client(initial))

    # New star appears at the front; page 1 now has a new repo + a known one.
    calls = {"n": 0}
    p2 = "https://api.github.com/user/starred?page=2"

    def handler(request):
        calls["n"] += 1
        if "page=2" not in str(request.url):
            return _page_response(request, [_repo(3, "c"), _repo(2, "b")], p2)
        # page 2 is entirely known -> should trigger early stop
        return _page_response(request, [_repo(1, "a")], None)

    result = sync_stars(conn, _client(handler))
    assert result["mode"] == "incremental"
    assert result["synced"] == 1
    assert db.repo_count(conn) == 3
    assert calls["n"] == 2  # stopped after the first all-known page


def test_resume_after_interrupt(conn):
    """A sync left in_progress resumes without early-stop and dedupes."""
    # Simulate interrupted first run: one repo committed, status in_progress.
    db.set_meta(conn, "last_sync_status", "in_progress")
    conn.execute(
        "INSERT INTO repos(id, full_name, starred_at) VALUES (1,'owner/a','x')")
    conn.commit()

    p2 = "https://api.github.com/user/starred?page=2"

    def handler(request):
        if "page=2" not in str(request.url):
            # page 1 all known — must NOT stop early in resume mode
            return _page_response(request, [_repo(1, "a")], p2)
        return _page_response(request, [_repo(2, "b")], None)

    result = sync_stars(conn, _client(handler))
    assert result["mode"] == "full"  # resume walks fully
    assert db.repo_count(conn) == 2
    assert result["synced"] == 1  # only repo 2 is new


def test_rate_limit_429_then_retry(conn):
    calls = {"n": 0}
    sleeps = []

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429, json={}, request=request,
                headers={"retry-after": "3"})
        return _page_response(request, [_repo(1, "a")], None)

    result = sync_stars(conn, _client(handler, sleeps))
    assert db.repo_count(conn) == 1
    assert result["synced"] == 1
    assert 3 in sleeps  # honored retry-after before continuing


def test_rate_limit_reset_wait(conn):
    """403 with remaining=0 waits until x-ratelimit-reset."""
    calls = {"n": 0}
    sleeps = []

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                403, json={}, request=request,
                headers={"x-ratelimit-remaining": "0",
                         "x-ratelimit-reset": "9999999999"})
        return _page_response(request, [_repo(1, "a")], None)

    sync_stars(conn, _client(handler, sleeps))
    assert db.repo_count(conn) == 1
    assert sleeps and sleeps[0] > 0  # slept toward reset


def test_missing_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(config.ConfigError, match="GITHUB_TOKEN not set"):
        config.github_token()


def test_invalid_token_401_not_retried(conn):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"message": "Bad credentials"},
                              request=request)

    with pytest.raises(GitHubError, match="401"):
        sync_stars(conn, _client(handler))
    assert calls["n"] == 1  # no retry on 401


def test_empty_account(conn):
    def handler(request):
        return _page_response(request, [], None)

    result = sync_stars(conn, _client(handler))
    assert result["synced"] == 0
    assert db.repo_count(conn) == 0
    assert db.get_meta(conn, "last_sync_status") == "completed"
