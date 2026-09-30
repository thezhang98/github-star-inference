"""Offline query_stars tests — in-memory SQLite, no GitHub/LLM calls.

Covers §六:
- 1: single-dimension filters (category / audience / niche / status / language /
     time / star range) each return the right rows
- 2: combined AND filters, incl. the two acceptance-question call paths
- 3: list total / count / limit / offset
- 4: aggregate over all six group_by keys, buckets count-desc, sum == total
- 5: niche/status computed at query time match M2's constants; unbackfilled
     repos still get niche/status
- 6: audience keyword normalization; NULL → 未知
- 7: boundary cases (empty DB, unbackfilled LEFT JOIN, NULL topics/pushed_at,
     illegal enum/mode/group_by, limit/offset clamping)
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from github_star import db
from github_star.query import GROUP_BYS, query_stars
from github_star.tag import _niche_bucket


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def _recent():
    return (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()


def _stale():
    return (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()


def _insert(conn, rid, full_name, stars=50, archived=0, pushed_at=None,
            starred_at="2024-01-01T00:00:00Z", language="Python",
            topics=("agent",), description="desc", html_url=None):
    owner, name = full_name.split("/")
    conn.execute(
        "INSERT INTO repos(id, full_name, name, owner, description, language, "
        "topics, stargazers_count, is_archived, pushed_at, starred_at, html_url)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, full_name, name, owner, description, language,
         json.dumps(list(topics)) if topics is not None else None,
         stars, archived, pushed_at if pushed_at is not None else _recent(),
         starred_at, html_url or f"https://github.com/{full_name}"),
    )
    conn.commit()


def _analyze(conn, rid, category=None, audience=None, summary=None):
    db.upsert_analysis(conn, rid, {
        "category": category, "audience": audience, "summary": summary,
    })
    conn.commit()


# --- empty DB (§七) ---------------------------------------------------------

def test_empty_db_list(conn):
    r = query_stars(conn, mode="list")
    assert r == {"mode": "list", "total": 0, "count": 0,
                 "limit": 50, "offset": 0, "items": []}


def test_empty_db_aggregate(conn):
    r = query_stars(conn, mode="aggregate", group_by="category")
    assert r["total"] == 0 and r["buckets"] == []


# --- LEFT JOIN keeps unbackfilled repos (§五, §六5) -------------------------

def test_unbackfilled_repo_not_dropped(conn):
    _insert(conn, 1, "o/raw", stars=42)  # no repo_analysis row at all
    r = query_stars(conn, mode="list")
    assert r["total"] == 1
    item = r["items"][0]
    assert item["category"] == "未分类"
    assert item["audience"] == "未知"
    assert item["niche"] == "极小众"   # still computed from stars
    assert item["status"] == "活跃"    # still computed from pushed_at


# --- niche computed at query time == M2 constants (§六5) --------------------

@pytest.mark.parametrize("stars,niche", [
    (0, "极小众"), (99, "极小众"), (100, "小众"), (999, "小众"),
    (1000, "成熟"), (9999, "成熟"), (10000, "主流"),
])
def test_niche_matches_m2(conn, stars, niche):
    _insert(conn, 1, "o/a", stars=stars)
    item = query_stars(conn, mode="list")["items"][0]
    assert item["niche"] == niche == _niche_bucket(stars)


# --- status computed at query time (§六5) -----------------------------------

def test_status_active(conn):
    _insert(conn, 1, "o/a", pushed_at=_recent())
    assert query_stars(conn)["items"][0]["status"] == "活跃"


def test_status_stale(conn):
    _insert(conn, 1, "o/a", pushed_at=_stale())
    assert query_stars(conn)["items"][0]["status"] == "停更"


def test_status_archived(conn):
    _insert(conn, 1, "o/a", archived=1, pushed_at=_recent())
    assert query_stars(conn)["items"][0]["status"] == "archived"


def test_status_null_pushed_at_is_stale(conn):
    # NULL push time is treated conservatively as 停更 (§二), unlike M2's 活跃
    conn.execute(
        "INSERT INTO repos(id, full_name, name, owner, stargazers_count, "
        "is_archived, pushed_at, starred_at, topics) "
        "VALUES (1,'o/a','a','o',5,0,NULL,'2024-01-01T00:00:00Z','[]')")
    conn.commit()
    assert query_stars(conn)["items"][0]["status"] == "停更"


# --- audience normalization (§六6) ------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("面向后端开发者", "开发者"),
    ("for developers building CLIs", "开发者"),
    ("UI/UX 设计师工具", "设计师"),
    ("DevOps and SRE teams", "运维"),
    ("学术研究人员", "研究者"),
    ("普通终端用户", "终端用户"),
    ("something unrelated", "终端用户"),
    (None, "未知"),
    ("", "未知"),
])
def test_audience_normalization(conn, text, expected):
    _insert(conn, 1, "o/a")
    _analyze(conn, 1, category="C", audience=text)
    assert query_stars(conn)["items"][0]["audience"] == expected


# --- single-dimension filters (§六1) ----------------------------------------

def _seed(conn):
    # 3 repos spanning categories, niches, languages, years
    _insert(conn, 1, "o/agent", stars=50, language="Python",
            starred_at="2024-05-01T00:00:00Z", pushed_at=_recent())
    _analyze(conn, 1, category="Agent 框架", audience="面向开发者")
    _insert(conn, 2, "o/web", stars=5000, language="TypeScript",
            starred_at="2022-05-01T00:00:00Z", pushed_at=_stale())
    _analyze(conn, 2, category="Web 框架", audience="设计师用")
    _insert(conn, 3, "o/tool", stars=200, language="Python",
            starred_at="2023-05-01T00:00:00Z", pushed_at=_recent())
    _analyze(conn, 3, category="Agent 框架", audience="研究者")


def test_filter_category(conn):
    _seed(conn)
    r = query_stars(conn, category=["Agent 框架"])
    assert r["total"] == 2
    assert {i["full_name"] for i in r["items"]} == {"o/agent", "o/tool"}


def test_filter_niche(conn):
    _seed(conn)
    r = query_stars(conn, niche=["极小众"])
    assert {i["full_name"] for i in r["items"]} == {"o/agent"}


def test_filter_niche_or(conn):
    _seed(conn)  # 极小众(50) + 小众(200) -> two repos
    r = query_stars(conn, niche=["极小众", "小众"])
    assert {i["full_name"] for i in r["items"]} == {"o/agent", "o/tool"}


def test_filter_status(conn):
    _seed(conn)
    r = query_stars(conn, status=["停更"])
    assert {i["full_name"] for i in r["items"]} == {"o/web"}


def test_filter_language(conn):
    _seed(conn)
    r = query_stars(conn, language=["Python"])
    assert r["total"] == 2


def test_filter_star_range(conn):
    _seed(conn)
    r = query_stars(conn, min_stars=100, max_stars=1000)
    assert {i["full_name"] for i in r["items"]} == {"o/tool"}


def test_filter_starred_after_year(conn):
    _seed(conn)
    r = query_stars(conn, starred_after="2023")
    assert {i["full_name"] for i in r["items"]} == {"o/agent", "o/tool"}


def test_filter_starred_before_year(conn):
    _seed(conn)
    r = query_stars(conn, starred_before="2023")
    assert {i["full_name"] for i in r["items"]} == {"o/web"}


def test_filter_audience(conn):
    _seed(conn)
    r = query_stars(conn, audience=["开发者"])
    assert {i["full_name"] for i in r["items"]} == {"o/agent"}


# --- acceptance questions (§六2, §四) ---------------------------------------

def test_acceptance_niche_agent_frameworks(conn):
    """'我 star 的小众 Agent 框架有哪些' — category + niche combined filter."""
    _seed(conn)
    r = query_stars(conn, mode="list", category=["Agent 框架"],
                    niche=["极小众", "小众"], sort="stars_desc")
    # both Agent 框架 repos are 极小众/小众; web (成熟) excluded by category anyway
    assert [i["full_name"] for i in r["items"]] == ["o/tool", "o/agent"]
    assert r["items"][0]["stargazers_count"] == 200  # stars_desc order


def test_acceptance_category_distribution_after_2023(conn):
    """'2023 年后我 star 的项目按类目怎么分布' — aggregate + time filter."""
    _seed(conn)
    r = query_stars(conn, mode="aggregate", group_by="category",
                    starred_after="2023")
    assert r["total"] == 2
    assert r["buckets"] == [{"key": "Agent 框架", "count": 2}]


# --- combined filter (§六2) -------------------------------------------------

def test_combined_and(conn):
    _seed(conn)
    r = query_stars(conn, category=["Agent 框架"], language=["Python"],
                    niche=["极小众"])
    assert {i["full_name"] for i in r["items"]} == {"o/agent"}


# --- pagination (§六3) ------------------------------------------------------

def test_pagination_total_count_limit_offset(conn):
    for i in range(1, 6):
        _insert(conn, i, f"o/r{i}", stars=i * 10)
    r = query_stars(conn, mode="list", sort="stars_asc", limit=2, offset=1)
    assert r["total"] == 5
    assert r["count"] == 2
    assert r["limit"] == 2 and r["offset"] == 1
    assert [i["full_name"] for i in r["items"]] == ["o/r2", "o/r3"]


def test_limit_clamped_to_max(conn):
    _insert(conn, 1, "o/a")
    r = query_stars(conn, limit=99999)
    assert r["limit"] == 500


def test_negative_limit_offset_clamped(conn):
    _insert(conn, 1, "o/a")
    r = query_stars(conn, limit=-5, offset=-3)
    assert r["limit"] == 0 and r["offset"] == 0 and r["count"] == 0


# --- aggregate over all group_by keys (§六4) --------------------------------

def test_aggregate_buckets_desc_and_sum(conn):
    _seed(conn)
    r = query_stars(conn, mode="aggregate", group_by="category")
    counts = [b["count"] for b in r["buckets"]]
    assert counts == sorted(counts, reverse=True)          # count desc
    assert sum(counts) == r["total"] == 3                  # sum == total
    assert r["buckets"][0] == {"key": "Agent 框架", "count": 2}


@pytest.mark.parametrize("group_by", GROUP_BYS)
def test_aggregate_every_group_by(conn, group_by):
    _seed(conn)
    r = query_stars(conn, mode="aggregate", group_by=group_by)
    assert sum(b["count"] for b in r["buckets"]) == r["total"] == 3


def test_aggregate_year_key(conn):
    _seed(conn)
    r = query_stars(conn, mode="aggregate", group_by="year")
    keys = {b["key"] for b in r["buckets"]}
    assert keys == {"2024", "2022", "2023"}


def test_aggregate_null_category_is_uncategorized(conn):
    _insert(conn, 1, "o/raw")  # unbackfilled
    r = query_stars(conn, mode="aggregate", group_by="category")
    assert r["buckets"] == [{"key": "未分类", "count": 1}]


# --- structured errors (§五, §六) -------------------------------------------

def test_error_bad_mode(conn):
    assert "error" in query_stars(conn, mode="nope")


def test_error_aggregate_missing_group_by(conn):
    assert "error" in query_stars(conn, mode="aggregate")


def test_error_bad_group_by(conn):
    assert "error" in query_stars(conn, mode="aggregate", group_by="nope")


def test_error_bad_niche_enum(conn):
    assert "error" in query_stars(conn, niche=["巨众"])


def test_error_bad_status_enum(conn):
    assert "error" in query_stars(conn, status=["半死"])


def test_error_bad_sort(conn):
    assert "error" in query_stars(conn, sort="nope")


# --- NULL topics tolerance (§五) --------------------------------------------

def test_null_topics_becomes_empty_list(conn):
    _insert(conn, 1, "o/a", topics=None)
    assert query_stars(conn)["items"][0]["topics"] == []


def test_summary_omitted_when_absent(conn):
    _insert(conn, 1, "o/a")
    assert "summary" not in query_stars(conn)["items"][0]
    _insert(conn, 2, "o/b")
    _analyze(conn, 2, category="C", summary="一句话")
    item = [i for i in query_stars(conn)["items"] if i["full_name"] == "o/b"][0]
    assert item["summary"] == "一句话"
