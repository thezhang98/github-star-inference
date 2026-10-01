"""Offline tag_repos tests — mock LLM + GitHub, no real API calls.

Covers the offline-verifiable items from §六:
- 1/2: zero LLM calls on rerun of already-tagged repos; resume from a phase
- 3:   categories land in 20–40; every tagged repo gets a category in the set
- 4:   niche_bucket / status_class computed by rule
- 5:   README truncation; empty description+README falls back, doesn't crash
- 6:   LLM failure on one repo -> skipped & left untagged, batch survives
- 8:   JSON parse tolerance; cluster count guardrail

Real end-to-end LLM tagging is left for key-bearing verification (§六8), per M1.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from github_star import db
from github_star.llm import LLMClient, LLMError, _parse_json
from github_star.tag import (_niche_bucket, _status_class, tag_repos,
                             HARD_MAX, TARGET_MAX, TARGET_MIN)


# --- fakes ------------------------------------------------------------------

class FakeGitHub:
    """Returns a canned README per repo; counts fetches."""
    def __init__(self, readmes=None, fail_on=None):
        self._readmes = readmes or {}
        self._fail_on = fail_on or set()
        self.calls = 0

    def get_readme(self, owner, name):
        self.calls += 1
        full = f"{owner}/{name}"
        if full in self._fail_on:
            raise RuntimeError("boom")
        return self._readmes.get(full)


def _fake_llm(scripted, model="fake-model", fail_repos=None):
    """LLMClient whose `complete` dispatches on message content.

    `scripted` maps a phase key ('free'/'cluster'/'backfill') to a function
    (user_text) -> dict. Raising inside simulates bad output; the client's own
    retry then surfaces LLMError, exercising failure isolation.
    """
    fail_repos = fail_repos or set()

    def complete(messages):
        system = messages[0]["content"]
        user = messages[1]["content"]
        if "聚类" in system:
            return json.dumps(scripted["cluster"](user))
        if "归类" in system:
            return json.dumps(scripted["backfill"](user))
        # free tagging
        for r in fail_repos:
            if r in user:
                raise ValueError("bad json from model")
        return json.dumps(scripted["free"](user))

    # retries=1 keeps failing-repo tests fast (no backoff sleeps)
    return LLMClient(model, complete, sleep=lambda s: None, retries=1)


def _insert_repo(conn, rid, full_name, stars=50, readme=None,
                 archived=0, pushed_at="2025-01-01T00:00:00Z",
                 description="desc", topics=("a",), language="Python"):
    owner, name = full_name.split("/")
    conn.execute(
        "INSERT INTO repos(id, full_name, name, owner, description, language, "
        "topics, stargazers_count, is_archived, pushed_at, starred_at, readme) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, full_name, name, owner, description, language,
         json.dumps(list(topics)), stars, archived, pushed_at,
         "2025-01-01T00:00:00Z", readme),
    )
    conn.commit()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


# --- pure-script helpers (§六4) ---------------------------------------------

@pytest.mark.parametrize("stars,bucket", [
    (0, "极小众"), (99, "极小众"), (100, "小众"), (999, "小众"),
    (1000, "成熟"), (9999, "成熟"), (10000, "主流"), (None, "极小众"),
])
def test_niche_bucket(stars, bucket):
    assert _niche_bucket(stars) == bucket


def test_status_class_archived():
    assert _status_class(1, "2025-01-01T00:00:00Z") == "archived"


def test_status_class_stale():
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    assert _status_class(0, old) == "停更"


def test_status_class_active():
    recent = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    assert _status_class(0, recent) == "活跃"


# --- JSON tolerance (§六8) --------------------------------------------------

def test_parse_json_plain():
    assert _parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_fenced():
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_with_prose():
    assert _parse_json('Sure! {"a": 1} done') == {"a": 1}


def test_llm_retries_then_raises():
    attempts = {"n": 0}

    def complete(messages):
        attempts["n"] += 1
        return "not json"

    client = LLMClient("m", complete, sleep=lambda s: None, retries=3)
    with pytest.raises(LLMError):
        client.chat_json("sys", "user")
    assert attempts["n"] == 3          # retried the configured number of times
    assert client.call_count == 1      # but counts as ONE logical call


# --- pipeline scripts -------------------------------------------------------

def _standard_scripts(n_categories=25):
    cats = [{"name": f"类目{i}", "description": "d", "member_tags": [f"t{i}"]}
            for i in range(n_categories)]

    return {
        "free": lambda u: {"tags": ["工具", "CLI"], "audience": "开发者",
                           "summary": "一个工具"},
        "cluster": lambda u: {"categories": cats},
        "backfill": lambda u: {"category": "类目0"},
    }


# --- full pipeline (§六1,3,4,5) ---------------------------------------------

def test_full_pipeline(conn):
    _insert_repo(conn, 1, "o/a", stars=5000)
    _insert_repo(conn, 2, "o/b", stars=50, archived=1)
    gh = FakeGitHub({"o/a": "# A", "o/b": "# B"})
    llm = _fake_llm(_standard_scripts(25))

    result = tag_repos(conn, gh, llm)

    assert result["tagged"] == 2
    assert TARGET_MIN <= result["categories"] <= TARGET_MAX
    assert result["backfilled"] == 2
    # every tagged repo has a category from the categories set (§六3)
    names = set(db.category_names(conn))
    rows = conn.execute(
        "SELECT category, niche_bucket, status_class FROM repo_analysis").fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r["category"] in names
    # niche/status by rule (§六4)
    a = conn.execute(
        "SELECT * FROM repo_analysis WHERE repo_id=1").fetchone()
    assert a["niche_bucket"] == "成熟"
    b = conn.execute(
        "SELECT * FROM repo_analysis WHERE repo_id=2").fetchone()
    assert b["status_class"] == "archived"
    assert db.get_meta(conn, "tag_phase") == "completed"


def test_readme_truncation(conn):
    _insert_repo(conn, 1, "o/a")
    gh = FakeGitHub({"o/a": "X" * 10000})
    llm = _fake_llm(_standard_scripts())
    tag_repos(conn, gh, llm, readme_max_chars=100)
    stored = conn.execute("SELECT readme FROM repos WHERE id=1").fetchone()["readme"]
    assert len(stored) == 100


def test_empty_description_and_readme_fallback(conn):
    _insert_repo(conn, 1, "o/a", description=None, topics=())
    gh = FakeGitHub({})  # 404 / no README -> None
    # model returns no tags -> pipeline must fall back, not crash
    scripts = _standard_scripts()
    scripts["free"] = lambda u: {"tags": [], "audience": "", "summary": ""}
    llm = _fake_llm(scripts)
    result = tag_repos(conn, gh, llm)
    assert result["tagged"] == 1
    tags = db.round1_tags_for(conn, 1)
    assert tags == ["uncategorized"]
    # README-less repo stored as "" (not NULL) so it isn't re-fetched
    assert conn.execute("SELECT readme FROM repos WHERE id=1").fetchone()["readme"] == ""


# --- zero-repeat LLM calls & resume (§六1,2) --------------------------------

def test_rerun_zero_llm_calls(conn):
    _insert_repo(conn, 1, "o/a")
    gh = FakeGitHub({"o/a": "# A"})
    tag_repos(conn, FakeGitHub({"o/a": "# A"}), _fake_llm(_standard_scripts()))

    # second run: nothing untagged/stale -> LLM must not be touched at all
    llm2 = _fake_llm(_standard_scripts())
    result = tag_repos(conn, gh, llm2, retag_stale=False)
    assert result["tagged"] == 0
    assert llm2.call_count == 0        # the assertable "zero repeat calls"


def test_stale_retag_deletes_old_tags(conn):
    _insert_repo(conn, 1, "o/a")
    tag_repos(conn, FakeGitHub({"o/a": "# A"}), _fake_llm(_standard_scripts()))
    # force staleness: push analyzed_at far into the past
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    conn.execute("UPDATE repo_analysis SET analyzed_at=? WHERE repo_id=1", (old,))
    conn.commit()

    scripts = _standard_scripts()
    scripts["free"] = lambda u: {"tags": ["新标签"], "audience": "x", "summary": "y"}
    llm2 = _fake_llm(scripts)
    result = tag_repos(conn, FakeGitHub({"o/a": "# A"}), llm2,
                       retag_stale=True, ttl_days=90)
    assert result["tagged"] == 1
    assert db.round1_tags_for(conn, 1) == ["新标签"]  # old tags gone


def test_not_stale_within_ttl_skips(conn):
    _insert_repo(conn, 1, "o/a")
    tag_repos(conn, FakeGitHub({"o/a": "# A"}), _fake_llm(_standard_scripts()))
    llm2 = _fake_llm(_standard_scripts())
    result = tag_repos(conn, FakeGitHub({"o/a": "# A"}), llm2,
                       retag_stale=True, ttl_days=90)  # fresh -> not stale
    assert result["tagged"] == 0
    assert llm2.call_count == 0


def test_stale_retag_refreshes_analyzed_at_and_converges(conn):
    """bug① regression: a stale repo re-tagged must get a fresh analyzed_at,
    so the NEXT retag_stale run sees it as fresh and makes zero LLM calls.
    Without the fix, analyzed_at stayed stale → infinite re-tagging."""
    _insert_repo(conn, 1, "o/a")
    tag_repos(conn, FakeGitHub({"o/a": "# A"}), _fake_llm(_standard_scripts()))
    # force staleness: analyzed_at 200 days ago
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    conn.execute("UPDATE repo_analysis SET analyzed_at=? WHERE repo_id=1", (old,))
    conn.commit()

    # run2: retag_stale re-tags the stale repo AND re-stamps analyzed_at
    llm2 = _fake_llm(_standard_scripts())
    r2 = tag_repos(conn, FakeGitHub({"o/a": "# A"}), llm2,
                   retag_stale=True, ttl_days=90)
    assert r2["tagged"] == 1
    refreshed = conn.execute(
        "SELECT analyzed_at FROM repo_analysis WHERE repo_id=1").fetchone()[0]
    assert refreshed != old
    assert refreshed > old  # ISO timestamps sort lexically → newer
    # repo re-entered backfill and got a category again
    assert conn.execute(
        "SELECT category FROM repo_analysis WHERE repo_id=1").fetchone()[0]

    # run3: now fresh → converged, zero LLM calls (no infinite re-tagging)
    llm3 = _fake_llm(_standard_scripts())
    r3 = tag_repos(conn, FakeGitHub({"o/a": "# A"}), llm3,
                   retag_stale=True, ttl_days=90)
    assert r3["tagged"] == 0
    assert llm3.call_count == 0


def test_incremental_run_no_dangling_category(conn):
    """bug② regression: adding new repos on a later run must not re-cluster and
    orphan existing repos' categories. Every repo's category must stay in the
    categories table even if the cluster script would return different names."""
    _insert_repo(conn, 1, "o/a")
    # run1: builds categories 类目0..类目24, backfills repo1
    tag_repos(conn, FakeGitHub({"o/a": "# A"}), _fake_llm(_standard_scripts()))
    cats_after_run1 = set(db.category_names(conn))

    # run2: a new repo appears; even if clustering *would* return a different
    # category set, the fix means it must NOT re-cluster (table non-empty).
    _insert_repo(conn, 2, "o/b")
    different = {
        "free": lambda u: {"tags": ["工具"], "audience": "x", "summary": "y"},
        # different names — must never be used, since clustering is skipped
        "cluster": lambda u: {"categories": [
            {"name": f"NEW{i}", "description": "d", "member_tags": [f"t{i}"]}
            for i in range(25)]},
        "backfill": lambda u: {"category": "类目0"},
    }
    llm2 = _fake_llm(different)
    tag_repos(conn, FakeGitHub({"o/b": "# B"}), llm2)

    # categories unchanged (no re-cluster), no NEW* names leaked in
    assert set(db.category_names(conn)) == cats_after_run1
    # every repo's category ∈ categories table (§六3, no dangling)
    names = set(db.category_names(conn))
    rows = conn.execute(
        "SELECT repo_id, category FROM repo_analysis").fetchall()
    assert len(rows) == 2
    for r in rows:
        assert r["category"] in names


# --- failure isolation (§六6) -----------------------------------------------

def test_one_repo_failure_isolated(conn):
    _insert_repo(conn, 1, "o/good", stars=100)
    _insert_repo(conn, 2, "o/bad", stars=100)
    gh = FakeGitHub({"o/good": "# G", "o/bad": "# B"})
    llm = _fake_llm(_standard_scripts(), fail_repos={"o/bad"})

    result = tag_repos(conn, gh, llm)
    assert result["tagged"] == 1
    assert result["tag_skipped"] == 1
    # good repo tagged; bad repo left untagged (no partial row)
    assert db.round1_tags_for(conn, 1)          # non-empty
    assert db.round1_tags_for(conn, 2) == []    # skipped, retried next run
    assert conn.execute(
        "SELECT category FROM repo_analysis WHERE repo_id=2").fetchone() is None


# --- cluster count guardrail (§六8/§五5) ------------------------------------

def test_cluster_reprompt_lands_in_band(conn):
    _insert_repo(conn, 1, "o/a")
    # first cluster call returns too many; reprompt returns a valid count
    state = {"n": 0}

    def cluster(user):
        state["n"] += 1
        count = HARD_MAX if state["n"] == 1 else 22
        return {"categories": [
            {"name": f"c{i}", "description": "d", "member_tags": [f"t{i}"]}
            for i in range(count)]}

    scripts = _standard_scripts()
    scripts["cluster"] = cluster
    scripts["backfill"] = lambda u: {"category": "c0"}
    llm = _fake_llm(scripts)

    result = tag_repos(conn, FakeGitHub({"o/a": "# A"}), llm)
    assert state["n"] == 2                      # reprompted once
    assert TARGET_MIN <= result["categories"] <= TARGET_MAX


def test_backfill_off_catalog_snaps_to_valid(conn):
    _insert_repo(conn, 1, "o/a")
    scripts = _standard_scripts()
    scripts["backfill"] = lambda u: {"category": "不存在的类目"}
    llm = _fake_llm(scripts)
    tag_repos(conn, FakeGitHub({"o/a": "# A"}), llm)
    cat = conn.execute(
        "SELECT category FROM repo_analysis WHERE repo_id=1").fetchone()["category"]
    assert cat in set(db.category_names(conn))  # snapped onto the catalog


def test_cluster_input_bounded_to_top_tags(conn):
    """P0 regression (real-data): a large distinct-tag set must not all be sent
    to the cluster call — hundreds of repos yield 1000+ one-off tags, which made
    the model loop and overflow its JSON. Only the top CLUSTER_MAX_TAGS go in."""
    from github_star.tag import CLUSTER_MAX_TAGS
    # one repo per distinct free tag → far more distinct tags than the bound
    n = CLUSTER_MAX_TAGS + 50
    for i in range(n):
        _insert_repo(conn, i + 1, f"o/r{i}")

    seen = {}
    scripts = _standard_scripts(25)
    scripts["free"] = lambda u, i=iter(range(n)): {
        "tags": [f"tag{next(i)}"], "audience": "开发者", "summary": "s"}

    def cluster(user):
        seen["tag_lines"] = sum(1 for ln in user.splitlines() if ln.endswith(")"))
        return {"categories": [{"name": f"类目{k}", "description": "d",
                                "member_tags": []} for k in range(25)]}
    scripts["cluster"] = cluster

    readmes = {f"o/r{i}": "# r" for i in range(n)}
    result = tag_repos(conn, FakeGitHub(readmes), _fake_llm(scripts))

    assert seen["tag_lines"] <= CLUSTER_MAX_TAGS   # the long tail was dropped
    assert result["phase"] == "completed"          # no crash on the big set
