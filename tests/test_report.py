"""Offline generate_report tests — in-memory SQLite + fake LLM, no network.

Covers the M4 acceptance list (验收 1-7):
- empty / untagged DB raises instead of a shell report (边界 case)
- 5-section report renders; counts come from the DB, not fabricated
- 小众宝藏 gate: no fork / archived / 停更 / stale-beyond-window leaks in (边界一)
- 遗产项目 splits archived vs 停更, both from stored status_class (边界二)
- combo anti-fabrication (边界三 / 验收4): a combo referencing an out-of-library
  repo id is DROPPED; dedup + per-repo reuse cap hold
- unverified 文案 is reproduced verbatim with no search backend (边界四)
- server-side verification fills evidence + conclusion when a search fn is wired
- one call returns both report + ideas; write_to drops report.md / ideas.md
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from github_star import db
from github_star.llm import LLMClient
from github_star.report import (MIN_IDEAS, UNVERIFIED_NOTE, ReportError,
                                generate_report)

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _days_ago(n):
    return (NOW - timedelta(days=n)).isoformat()


def _insert(conn, rid, full_name, *, stars=50, is_fork=0, is_archived=0,
            pushed_at=None, repo_created_at=None, starred_at="2024-01-01T00:00:00Z",
            language="Python", topics=("agent",), description="一个工具",
            readme="# readme\n内容", forks_count=3, html_url=None):
    owner, name = full_name.split("/")
    conn.execute(
        "INSERT INTO repos(id, full_name, name, owner, description, language, "
        "topics, stargazers_count, forks_count, is_fork, is_archived, pushed_at, "
        "repo_created_at, starred_at, readme, html_url) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, full_name, name, owner, description, language,
         json.dumps(list(topics)) if topics is not None else None,
         stars, forks_count, is_fork, is_archived,
         pushed_at if pushed_at is not None else _days_ago(10),
         repo_created_at if repo_created_at is not None else _days_ago(400),
         starred_at, readme, html_url or f"https://github.com/{full_name}"),
    )
    conn.commit()


def _analyze(conn, rid, *, category="工具", audience="面向开发者",
             niche_bucket="小众", status_class="活跃", summary="一句话"):
    db.upsert_analysis(conn, rid, {
        "category": category, "audience": audience, "niche_bucket": niche_bucket,
        "status_class": status_class, "summary": summary,
    })
    conn.commit()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.execute("INSERT INTO categories(name, description) VALUES ('工具', '实用工具')")
    c.commit()
    yield c
    c.close()


# --- fake LLM ---------------------------------------------------------------

def _fake_llm(ideas=None, verdicts=None):
    """LLMClient dispatching on system prompt: combo-discovery vs legacy-verdict.

    `ideas` is the dict the discovery call returns ({derivative, combos});
    `verdicts` is the legacy-verdict list. Both default to empty.
    """
    def complete(messages):
        system = messages[0]["content"]
        if "考古" in system:
            return json.dumps({"verdicts": verdicts or []})
        return json.dumps(ideas or {"derivative": [], "combos": []})
    return LLMClient("fake", complete, sleep=lambda s: None, retries=1)


# --- empty / untagged DB (边界 case) ----------------------------------------

def test_empty_db_raises(conn):
    with pytest.raises(ReportError):
        generate_report(conn, _fake_llm(), now=NOW)


def test_synced_but_untagged_raises(conn):
    _insert(conn, 1, "o/a")  # repos row but no repo_analysis
    with pytest.raises(ReportError):
        generate_report(conn, _fake_llm(), now=NOW)


# --- report shape (验收1) ---------------------------------------------------

def test_five_sections_present(conn):
    _insert(conn, 1, "o/a")
    _analyze(conn, 1)
    out = generate_report(conn, _fake_llm(), now=NOW)
    md = out["report_markdown"]
    for heading in ("## 一、总览", "## 二、功能用途细分类目全景",
                    "小众宝藏", "## 四、遗产项目", "兴趣演变时间线"):
        assert heading in md
    assert out["meta"]["total_repos"] == 1
    assert out["meta"]["analyzed_repos"] == 1


# --- 小众宝藏 gate (验收2, 边界一) -------------------------------------------

def test_treasure_excludes_fork_archived_stale(conn):
    # qualifies: small, active, non-fork, has readme + description
    _insert(conn, 1, "o/gem", stars=300, is_fork=0, pushed_at=_days_ago(30))
    _analyze(conn, 1, niche_bucket="小众", status_class="活跃")
    # fork -> excluded
    _insert(conn, 2, "o/fork", stars=200, is_fork=1, pushed_at=_days_ago(30))
    _analyze(conn, 2, niche_bucket="小众", status_class="活跃")
    # archived -> excluded (status_class not 活跃)
    _insert(conn, 3, "o/arch", stars=200, is_archived=1, pushed_at=_days_ago(30))
    _analyze(conn, 3, niche_bucket="小众", status_class="archived")
    # 停更 snapshot -> excluded
    _insert(conn, 4, "o/stale", stars=200, pushed_at=_days_ago(500))
    _analyze(conn, 4, niche_bucket="小众", status_class="停更")
    # mainstream (niche 成熟) -> excluded
    _insert(conn, 5, "o/big", stars=5000, pushed_at=_days_ago(30))
    _analyze(conn, 5, niche_bucket="成熟", status_class="活跃")
    # pushed beyond ACTIVE_RECENT_DAYS window but status still 活跃 -> excluded
    _insert(conn, 6, "o/dozed", stars=200, pushed_at=_days_ago(300))
    _analyze(conn, 6, niche_bucket="小众", status_class="活跃")
    # missing description -> excluded (信号不足)
    _insert(conn, 7, "o/nodesc", stars=200, description="", pushed_at=_days_ago(30))
    _analyze(conn, 7, niche_bucket="小众", status_class="活跃")

    md = generate_report(conn, _fake_llm(), now=NOW,
                         active_recent_days=180)["report_markdown"]
    treasure_block = md.split("小众宝藏")[1].split("## 四")[0]
    assert "o/gem" in treasure_block
    for excluded in ("o/fork", "o/arch", "o/stale", "o/big", "o/dozed", "o/nodesc"):
        assert excluded not in treasure_block


def test_treasure_empty_shows_wu(conn):
    _insert(conn, 1, "o/big", stars=5000)
    _analyze(conn, 1, niche_bucket="成熟")
    md = generate_report(conn, _fake_llm(), now=NOW)["report_markdown"]
    treasure_block = md.split("小众宝藏")[1].split("## 四")[0]
    assert "无" in treasure_block


# --- 遗产项目 split (验收1, 边界二) ------------------------------------------

def test_legacy_splits_archived_and_stale(conn):
    _insert(conn, 1, "o/arch", is_archived=1)
    _analyze(conn, 1, status_class="archived")
    _insert(conn, 2, "o/stale", pushed_at=_days_ago(500))
    _analyze(conn, 2, status_class="停更")
    verdicts = [{"id": 1, "归宿": "有现代替代品", "一句话": "被 X 取代"},
                {"id": 2, "归宿": "值得复活", "一句话": "思路仍新"}]
    md = generate_report(conn, _fake_llm(verdicts=verdicts),
                         now=NOW)["report_markdown"]
    legacy = md.split("## 四、遗产项目")[1].split("## 五")[0]
    assert "已归档 archived（1 个）" in legacy
    assert "停更 >365 天（1 个）" in legacy
    assert "有现代替代品" in legacy and "值得复活" in legacy


def test_legacy_verdict_outside_library_ignored(conn):
    _insert(conn, 1, "o/arch", is_archived=1)
    _analyze(conn, 1, status_class="archived")
    # verdict references id 999 (not in DB) -> ignored, repo 1 defaults 纯遗产
    verdicts = [{"id": 999, "归宿": "值得复活", "一句话": "幽灵仓库"}]
    md = generate_report(conn, _fake_llm(verdicts=verdicts),
                         now=NOW)["report_markdown"]
    legacy = md.split("## 四、遗产项目")[1].split("## 五")[0]
    assert "纯遗产" in legacy
    assert "幽灵仓库" not in legacy


# --- combo anti-fabrication (验收4, 边界三) — the headline test --------------

def test_combo_with_out_of_library_repo_is_dropped(conn):
    _insert(conn, 1, "o/a")
    _analyze(conn, 1)
    _insert(conn, 2, "o/b")
    _analyze(conn, 2)
    ideas = {"derivative": [], "combos": [
        {"方向": "合法组合", "组合来源": [1, 2], "解决的问题": "p",
         "类型": "t", "可行性": "高 理由"},
        {"方向": "编造组合", "组合来源": [1, 777], "解决的问题": "p",
         "类型": "t", "可行性": "高 理由"},   # 777 not in library -> drop
        {"方向": "全编造", "组合来源": [888, 999], "解决的问题": "p",
         "类型": "t", "可行性": "高 理由"},   # both missing -> drop
    ]}
    out = generate_report(conn, _fake_llm(ideas=ideas), now=NOW)
    assert out["meta"]["ideas"]["combos"] == 1
    ideas_md = out["ideas_markdown"]
    assert "合法组合" in ideas_md
    assert "编造组合" not in ideas_md and "全编造" not in ideas_md
    # the surviving combo references only in-library repos
    assert "o/a" in ideas_md and "o/b" in ideas_md


def test_combo_dedup_and_reuse_cap(conn):
    for i in range(1, 6):
        _insert(conn, i, f"o/r{i}")
        _analyze(conn, i)
    ideas = {"derivative": [], "combos": [
        {"方向": "c1", "组合来源": [1, 2], "解决的问题": "p", "类型": "t", "可行性": "高"},
        {"方向": "dup", "组合来源": [2, 1], "解决的问题": "p", "类型": "t", "可行性": "高"},  # same unordered pair
        {"方向": "c2", "组合来源": [1, 3], "解决的问题": "p", "类型": "t", "可行性": "高"},
        {"方向": "c3", "组合来源": [1, 4], "解决的问题": "p", "类型": "t", "可行性": "高"},  # repo 1 now in 3 pairs -> capped
    ]}
    out = generate_report(conn, _fake_llm(ideas=ideas), now=NOW)
    md = out["ideas_markdown"]
    assert "c1" in md and "c2" in md
    assert "dup" not in md       # unordered-pair dedup
    assert "c3" not in md        # repo 1 reuse capped at 2


def test_derivative_without_support_dropped(conn):
    _insert(conn, 1, "o/a")
    _analyze(conn, 1)
    ideas = {"derivative": [
        {"方向": "有支撑", "数据支撑": [1], "类型": "t", "可行性": "高"},
        {"方向": "无支撑", "数据支撑": [], "类型": "t", "可行性": "高"},
        {"方向": "库外支撑", "数据支撑": [404], "类型": "t", "可行性": "高"},
    ], "combos": []}
    out = generate_report(conn, _fake_llm(ideas=ideas), now=NOW)
    md = out["ideas_markdown"]
    assert "有支撑" in md
    assert "无支撑" not in md and "库外支撑" not in md
    assert out["meta"]["ideas"]["derivative"] == 1


# --- unverified note verbatim (验收5, 边界四) --------------------------------

def test_unverified_note_verbatim_without_search(conn):
    _insert(conn, 1, "o/a")
    _analyze(conn, 1)
    _insert(conn, 2, "o/b")
    _analyze(conn, 2)
    ideas = {"derivative": [], "combos": [
        {"方向": "组合", "组合来源": [1, 2], "解决的问题": "p",
         "类型": "t", "可行性": "高"}]}
    out = generate_report(conn, _fake_llm(ideas=ideas), now=NOW, search=None)
    assert UNVERIFIED_NOTE in out["ideas_markdown"]
    assert out["meta"]["search_verified"] is False


def test_server_side_verification_fills_evidence(conn):
    _insert(conn, 1, "o/a")
    _analyze(conn, 1)
    _insert(conn, 2, "o/b")
    _analyze(conn, 2)
    ideas = {"derivative": [], "combos": [
        {"方向": "组合", "组合来源": [1, 2], "解决的问题": "有人要吗",
         "类型": "t", "可行性": "高"}]}
    calls = []

    def fake_search(q):
        calls.append(q)
        return {"evidence": ["https://e1", "https://e2"], "conclusion": "有真实需求"}

    out = generate_report(conn, _fake_llm(ideas=ideas), now=NOW,
                          search=fake_search)
    md = out["ideas_markdown"]
    assert "已验证" in md and "https://e1" in md and "有真实需求" in md
    assert UNVERIFIED_NOTE not in md
    assert calls == ["有人要吗"]
    assert out["meta"]["search_verified"] is True


def test_search_failure_degrades_to_unverified(conn):
    _insert(conn, 1, "o/a")
    _analyze(conn, 1)
    _insert(conn, 2, "o/b")
    _analyze(conn, 2)
    ideas = {"derivative": [], "combos": [
        {"方向": "组合", "组合来源": [1, 2], "解决的问题": "p",
         "类型": "t", "可行性": "高"}]}

    def boom(q):
        raise RuntimeError("network down")

    md = generate_report(conn, _fake_llm(ideas=ideas), now=NOW,
                         search=boom)["ideas_markdown"]
    assert UNVERIFIED_NOTE in md and "搜索请求失败" in md


# --- one call -> both outputs; write_to (验收6) -----------------------------

def test_one_call_returns_both_and_writes_files(conn, tmp_path):
    _insert(conn, 1, "o/a")
    _analyze(conn, 1)
    out = generate_report(conn, _fake_llm(), now=NOW, write_to=str(tmp_path))
    assert out["report_markdown"] and out["ideas_markdown"]
    assert (tmp_path / "report.md").read_text(encoding="utf-8")
    assert (tmp_path / "ideas.md").read_text(encoding="utf-8")
    assert out["meta"]["written_to"] == str(tmp_path)


# --- ≥10 ideas when the LLM supplies them (验收3) ----------------------------

def test_ten_plus_ideas_render(conn):
    for i in range(1, 9):
        _insert(conn, i, f"o/r{i}")
        _analyze(conn, i)
    derivative = [{"方向": f"衍生{i}", "数据支撑": [i], "类型": "t",
                   "可行性": "中"} for i in range(1, 7)]
    combos = [{"方向": f"组合{i}", "组合来源": [i, i + 1], "解决的问题": "p",
               "类型": "t", "可行性": "高"} for i in (1, 3, 5, 7)]
    out = generate_report(conn, _fake_llm(ideas={"derivative": derivative,
                                                 "combos": combos}), now=NOW)
    assert out["meta"]["ideas"]["total"] >= MIN_IDEAS
    assert out["meta"]["ideas"]["combos"] >= 4
