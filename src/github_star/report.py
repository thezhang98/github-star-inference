"""generate_report: one call -> 《Star 数据分析报告》(5 节) +《想法清单》.

Everything here is a *downstream reader* of M2/M3's stored work — it never
re-tags, never recomputes niche/status, and never builds a second query layer:

* aggregate / list sections go through M3's ``query_stars`` (and the enriched
  items it returns).
* the 小众宝藏 gate and 遗产项目 section read the **stored** ``repo_analysis``
  ``niche_bucket`` / ``status_class`` directly (边界一/二 say so verbatim — read
  the snapshot, do not recompute), plus the raw ``repos`` columns ``query_stars``
  does not expose (``is_fork`` / ``readme`` / ``forks_count`` / ``repo_created_at``).
* combo discovery + idea generation reuse ``llm.chat_json`` in a single batch.

Anti-fabrication is the hard gate (边界三 / 验收4): the LLM is handed a repo
catalog and told to reference repos **only by id**; every id it returns is
checked back against the library and any out-of-library reference drops the whole
idea. The LLM can never smuggle a repo that is not in the DB into the output.
"""
import json
import logging
from datetime import datetime, timezone

from . import db
from .llm import LLMError
from .query import query_stars

log = logging.getLogger("github_star.report")

# 未验证 note — reproduced verbatim from the M4 task spec (边界四). Do not reword:
# the client-dialogue handoff keys off this exact text.
UNVERIFIED_NOTE = (
    "未验证——本条组合为 LLM 基于库内数据生成的设想，真实需求待在 MCP 客户端对话中用"
    "联网搜索核实（或配置 SEARCH_PROVIDER + SEARCH_API_KEY 后由 server 自动验证）。"
)

TREASURE_TOP_N = 20
MIN_IDEAS = 10
MIN_COMBOS = 4
MAX_REPO_IN_COMBOS = 2          # one repo appears in at most 2 pairs (边界三 去重)
CANDIDATE_POOL = 120            # repos handed to the LLM as the combo catalog

_COMBO_SYSTEM = (
    "你是开源生态分析师。给定一份【库内仓库目录】(每行: id | full_name | 一句话介绍)，"
    "基于这些真实仓库产出两类想法，输出严格 JSON。"
    "**铁律：所有仓库引用只能使用目录中给出的 id 整数，严禁虚构 id 或仓库名；"
    "引用目录外的 id 该条会被丢弃。**\n"
    "输出格式：\n"
    '{"derivative": [{"方向": "一句话", "数据支撑": [id, ...], "类型": "一句话", '
    '"可行性": "高|中|低 + 一句理由"}, ...],\n'
    ' "combos": [{"方向": "一句话", "组合来源": [idA, idB], "解决的问题": "一句话", '
    '"类型": "一句话", "可行性": "高|中|低 + 一句理由"}, ...]}\n'
    "衍生型 数据支撑 至少指向 1 个库内 id；组合型 组合来源 必须恰好 2 个不同的库内 id。"
    "只输出 JSON，不要解释。"
)

_LEGACY_SYSTEM = (
    "你是开源项目考古助手。给定一批【已归档/停更的库内仓库】(id | full_name | 介绍)，"
    "为每个仓库判定一句话归宿，判断只针对该仓库本身，严禁引入目录外的其它项目。"
    "输出严格 JSON：{\"verdicts\": [{\"id\": id, \"归宿\": \"值得复活|有现代替代品|纯遗产\", "
    "\"一句话\": \"理由\"}, ...]}。id 必须来自给定目录。只输出 JSON。"


)


def _now(now=None) -> datetime:
    return now or datetime.now(timezone.utc)


def _parse_dt(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _topics(raw):
    try:
        return json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []


class ReportError(RuntimeError):
    """Raised when the library is empty / untagged — no shell report (边界 case)."""


# --- data pulls -------------------------------------------------------------

def _snapshot_time(conn) -> str:
    """Best available data-snapshot timestamp for the overview section."""
    return (db.get_meta(conn, "last_sync_completed_at")
            or db.get_meta(conn, "last_tag_completed_at")
            or _now().isoformat())


def _treasures(conn, active_recent_days, now):
    """小众宝藏候选 (边界一): hard gates in SQL on stored snapshot columns,
    recency gate + star-velocity ranking in Python. No recompute of niche/status.
    """
    rows = conn.execute(
        """
        SELECT r.id, r.full_name, r.description, r.html_url, r.language,
               r.topics, r.stargazers_count, r.forks_count, r.pushed_at,
               r.repo_created_at, a.category, a.summary
        FROM repos r JOIN repo_analysis a ON a.repo_id = r.id
        WHERE a.niche_bucket IN ('极小众', '小众')   -- stars < 1000, 复用已落库值
          AND r.is_fork = 0                          -- fork 非原创, 排除
          AND a.status_class = '活跃'                 -- archived/停更 已被排除
          AND r.readme IS NOT NULL AND r.readme != ''
          AND r.description IS NOT NULL AND r.description != ''
        """
    ).fetchall()

    cutoff_days = active_recent_days
    out = []
    for r in rows:
        pushed = _parse_dt(r["pushed_at"])
        if pushed is None or (now - pushed).days > cutoff_days:
            continue  # 比 status_class 更紧的独立近期活跃阈值 (边界一)
        created = _parse_dt(r["repo_created_at"])
        age_months = 1.0
        if created:
            age_months = max(1.0, (now - created).days / 30.0)
        stars = r["stargazers_count"] or 0
        topics = _topics(r["topics"])
        out.append({
            "id": r["id"],
            "full_name": r["full_name"],
            "description": r["description"],
            "html_url": r["html_url"],
            "language": r["language"],
            "stargazers_count": stars,
            "forks_count": r["forks_count"] or 0,
            "pushed_at": r["pushed_at"],
            "topics": topics,
            "category": r["category"],
            "summary": r["summary"],
            "velocity": stars / age_months,
            "_bonus": (1 if topics else 0) + (1 if (r["forks_count"] or 0) > 0 else 0),
        })
    # 主排序 velocity 降序；次排序 pushed_at 更近；并列加分信号 (边界一)
    out.sort(key=lambda t: (t["velocity"], t["pushed_at"] or "", t["_bonus"]),
             reverse=True)
    return out[:TREASURE_TOP_N]


def _legacy(conn):
    """遗产项目 (边界二): stored status_class IN (archived, 停更), split in two."""
    rows = conn.execute(
        """
        SELECT r.id, r.full_name, r.description, r.html_url, r.stargazers_count,
               r.pushed_at, a.status_class, a.category, a.summary
        FROM repos r JOIN repo_analysis a ON a.repo_id = r.id
        WHERE a.status_class IN ('archived', '停更')
        ORDER BY r.stargazers_count DESC
        """
    ).fetchall()
    archived, stale = [], []
    for r in rows:
        item = {
            "id": r["id"], "full_name": r["full_name"],
            "description": r["description"] or r["summary"] or "",
            "html_url": r["html_url"], "stargazers_count": r["stargazers_count"] or 0,
            "pushed_at": r["pushed_at"], "category": r["category"],
        }
        (archived if r["status_class"] == "archived" else stale).append(item)
    return archived, stale


def _candidate_pool(conn):
    """库内仓库目录 fed to the LLM for combo discovery / idea generation.

    Reuses query_stars (list mode) so the pool shares M3's enrichment and no
    second query path appears. id is pulled alongside so the LLM can reference
    repos by id and we can validate the references back against the library.
    """
    res = query_stars(conn, mode="list", sort="stars_desc", limit=CANDIDATE_POOL)
    items = res.get("items", [])
    # query_stars items carry full_name but not id; map full_name -> id once.
    name_to_id = {r["full_name"]: r["id"] for r in conn.execute(
        "SELECT id, full_name FROM repos").fetchall()}
    pool = []
    for it in items:
        rid = name_to_id.get(it["full_name"])
        if rid is None:
            continue
        blurb = it.get("summary") or it.get("description") or ""
        pool.append({"id": rid, "full_name": it["full_name"],
                     "blurb": blurb, "item": it})
    return pool


# --- LLM steps --------------------------------------------------------------

def _legacy_verdicts(llm, archived, stale):
    """One best-effort LLM call -> {repo_id: (归宿, 一句话)}. Default 纯遗产."""
    repos = archived + stale
    if not repos or llm is None:
        return {}
    valid_ids = {r["id"] for r in repos}
    lines = "\n".join(f'{r["id"]} | {r["full_name"]} | {r["description"][:120]}'
                      for r in repos)
    try:
        out = llm.chat_json(_LEGACY_SYSTEM, f"仓库目录：\n{lines}")
    except LLMError as exc:
        log.warning("legacy verdict LLM call failed, defaulting 纯遗产: %s", exc)
        return {}
    verdicts = {}
    for v in (out.get("verdicts") or []):
        try:
            rid = int(v.get("id"))
        except (TypeError, ValueError):
            continue
        if rid not in valid_ids:          # 库外引用直接忽略
            continue
        label = (v.get("归宿") or "").strip()
        if label not in ("值得复活", "有现代替代品", "纯遗产"):
            label = "纯遗产"
        verdicts[rid] = (label, (v.get("一句话") or "").strip())
    return verdicts


def _discover_ideas(llm, pool, max_combos):
    """One LLM batch -> validated (derivative_ideas, combo_ideas).

    Every repo id the LLM returns is checked against ``pool`` ids; any
    out-of-library reference drops that idea entirely (anti-fabrication, 边界三).
    """
    if llm is None or not pool:
        return [], []
    valid = {p["id"]: p for p in pool}
    catalog = "\n".join(f'{p["id"]} | {p["full_name"]} | {p["blurb"][:120]}'
                        for p in pool)
    user = (f"库内仓库目录（共 {len(pool)} 个，只能引用这些 id）：\n{catalog}\n\n"
            f"请产出衍生型想法与至多 {max_combos} 对组合型想法。")
    try:
        out = llm.chat_json(_COMBO_SYSTEM, user)
    except LLMError as exc:
        log.warning("idea discovery LLM call failed: %s", exc)
        return [], []

    # derivative: keep only repo refs that exist; drop ideas with zero support.
    derivative = []
    for d in (out.get("derivative") or []):
        refs = [valid[i] for i in _as_ids(d.get("数据支撑")) if i in valid]
        if not refs:
            continue  # 无库内数据支撑 -> 宁缺毋滥, 丢弃
        derivative.append({
            "方向": (d.get("方向") or "").strip(),
            "类型": (d.get("类型") or "").strip(),
            "可行性": (d.get("可行性") or "").strip(),
            "支撑": refs,
        })

    # combos: both ids must exist; dedup unordered pairs; cap repo reuse.
    combos = []
    seen_pairs = set()
    repo_use = {}
    for c in (out.get("combos") or []):
        ids = [i for i in _as_ids(c.get("组合来源")) if i in valid]
        ids = list(dict.fromkeys(ids))        # drop dup id within the pair
        if len(ids) != 2:                     # 库外引用 / 自配对 -> 丢弃整条
            continue
        key = tuple(sorted(ids))
        if key in seen_pairs:
            continue
        if any(repo_use.get(i, 0) >= MAX_REPO_IN_COMBOS for i in ids):
            continue                          # 单 repo 最多出现在 2 对里
        seen_pairs.add(key)
        for i in ids:
            repo_use[i] = repo_use.get(i, 0) + 1
        combos.append({
            "方向": (c.get("方向") or "").strip(),
            "解决的问题": (c.get("解决的问题") or "").strip(),
            "类型": (c.get("类型") or "").strip(),
            "可行性": (c.get("可行性") or "").strip(),
            "A": valid[key[0]], "B": valid[key[1]],
        })
        if len(combos) >= max_combos:
            break
    return derivative, combos


def _as_ids(raw):
    """Coerce an LLM-returned id list into ints, tolerating str ids / None."""
    ids = []
    for x in (raw or []):
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    return ids


# --- markdown rendering -----------------------------------------------------

def _fmt_repo(it) -> str:
    return f'[{it["full_name"]}]({it["html_url"]})'


def _render_report(conn, meta, lang_buckets, cat_buckets, cat_desc,
                   panorama, treasures, archived, stale, verdicts, timeline,
                   active_recent_days) -> str:
    L = []
    L.append("# Star 数据分析报告\n")

    # 1 总览
    L.append("## 一、总览\n")
    L.append(f"- Star 项目总数：**{meta['total_repos']}**")
    L.append(f"- 已分析（打标）项目数：**{meta['analyzed_repos']}**")
    L.append(f"- 数据快照时间：{meta['snapshot_at']}")
    if lang_buckets:
        top_lang = "、".join(f'{b["key"]} {b["count"]}' for b in lang_buckets[:10])
        L.append(f"- 语言分布（Top 10）：{top_lang}")
    else:
        L.append("- 语言分布：无")
    L.append("")

    # 2 功能用途细分类目全景
    L.append("## 二、功能用途细分类目全景\n")
    if not cat_buckets:
        L.append("无\n")
    else:
        for b in cat_buckets:
            name, cnt = b["key"], b["count"]
            desc = cat_desc.get(name, "")
            L.append(f"### {name}（{cnt} 个）")
            if desc:
                L.append(f"_{desc}_")
            tops = panorama.get(name, [])
            if tops:
                for t in tops:
                    L.append(f"- {_fmt_repo(t)} ⭐{t['stargazers_count']}"
                             f" — {t.get('summary') or t.get('description') or ''}")
            else:
                L.append("- 无")
            L.append("")

    # 3 小众宝藏 Top 20
    L.append(f"## 三、小众宝藏 Top {len(treasures)}"
             f"（stars<1000 且 {active_recent_days} 天内活跃、非 fork、非停更）\n")
    if not treasures:
        L.append("无\n")
    else:
        L.append("| # | 项目 | ⭐ | 语言 | 近推时间 | 增速(⭐/月) | 一句话 |")
        L.append("|---|------|----|------|----------|-----------|--------|")
        for i, t in enumerate(treasures, 1):
            pushed = (t["pushed_at"] or "")[:10]
            blurb = (t.get("summary") or t.get("description") or "").replace("|", "/")
            L.append(f'| {i} | {_fmt_repo(t)} | {t["stargazers_count"]} |'
                     f' {t["language"] or "-"} | {pushed} |'
                     f' {t["velocity"]:.1f} | {blurb} |')
        L.append("")

    # 4 遗产项目
    L.append("## 四、遗产项目\n")
    L.append(f"### 已归档 archived（{len(archived)} 个）")
    _render_legacy_block(L, archived, verdicts)
    L.append(f"### 停更 >365 天（{len(stale)} 个）")
    _render_legacy_block(L, stale, verdicts)

    # 5 兴趣演变时间线
    L.append("## 五、兴趣演变时间线（按 star 年份聚合类目）\n")
    if not timeline:
        L.append("无\n")
    else:
        for year in sorted(timeline, reverse=True):
            cats = timeline[year]
            top = "、".join(f"{name}×{cnt}" for name, cnt in cats[:5])
            total = sum(c for _, c in cats)
            L.append(f"- **{year}**（{total} 个）：{top}")
        L.append("")

    return "\n".join(L).rstrip() + "\n"


def _render_legacy_block(L, items, verdicts):
    if not items:
        L.append("- 无\n")
        return
    for it in items:
        label, reason = verdicts.get(it["id"], ("纯遗产", ""))
        tail = f" — {reason}" if reason else ""
        L.append(f'- {_fmt_repo(it)} ⭐{it["stargazers_count"]} '
                 f'【{label}】{tail}')
    L.append("")


def _render_ideas(derivative, combos, search) -> str:
    L = ["# 想法清单\n"]
    total = len(derivative) + len(combos)
    L.append(f"共 {total} 条（衍生型 {len(derivative)} + 组合型 {len(combos)}）。"
             "所有引用的仓库均来自库内真实数据。\n")

    L.append("## 衍生型\n")
    if not derivative:
        L.append("无\n")
    for i, d in enumerate(derivative, 1):
        support = "、".join(_fmt_repo(r["item"]) for r in d["支撑"])
        L.append(f"**D{i}. {d['方向']}**")
        L.append(f"- 数据支撑：{support}")
        L.append(f"- 类型：{d['类型'] or '-'}")
        L.append(f"- 可行性：{d['可行性'] or '-'}\n")

    L.append("## 组合型\n")
    if not combos:
        L.append("无\n")
    for i, c in enumerate(combos, 1):
        a, b = _fmt_repo(c["A"]["item"]), _fmt_repo(c["B"]["item"])
        L.append(f"**C{i}. {c['方向']}**")
        L.append(f"- 组合来源：{a} ＋ {b}")
        L.append(f"- 解决的问题：{c['解决的问题'] or '-'}")
        L.append(f"- 类型：{c['类型'] or '-'}")
        L.append(f"- 可行性：{c['可行性'] or '-'}")
        L.append(f"- 需求验证：{_verify_line(c, search)}\n")

    return "\n".join(L).rstrip() + "\n"


def _verify_line(combo, search) -> str:
    """Server-side verification when a search backend is wired, else 未验证."""
    if search is None:
        return UNVERIFIED_NOTE
    query = combo["解决的问题"] or combo["方向"]
    try:
        res = search(query)
    except Exception as exc:  # noqa: BLE001 — degrade, never abort the report
        log.warning("search verify failed: %s", exc)
        return UNVERIFIED_NOTE + "（搜索请求失败）"
    urls = res.get("evidence") or []
    conclusion = (res.get("conclusion") or "").strip()
    ev = "，".join(urls[:3]) if urls else "无"
    return f"已验证——证据：{ev}；结论：{conclusion or '见证据'}"


# --- orchestration ----------------------------------------------------------

def generate_report(conn, llm=None, *, search=None, now=None, write_to=None,
                    active_recent_days=180, max_combo_candidates=15) -> dict:
    """Produce report + idea list in one pass. See module docstring.

    ``llm`` and ``search`` are injected so tests run fully offline. ``search``
    is ``None`` (client-dialogue channel -> 未验证) unless the server wires a
    backend. Returns ``{report_markdown, ideas_markdown, meta}``.
    """
    now = _now(now)
    if conn.execute("SELECT COUNT(*) AS c FROM repo_analysis").fetchone()["c"] == 0:
        raise ReportError("repo_analysis 为空：请先运行 sync_stars 再运行 tag_repos，"
                          "再调用 generate_report。")

    # overview + language distribution (authoritative counts, no 500 cap).
    lang_agg = query_stars(conn, mode="aggregate", group_by="language")
    cat_agg = query_stars(conn, mode="aggregate", group_by="category")
    lang_buckets = lang_agg.get("buckets", [])
    cat_buckets = cat_agg.get("buckets", [])
    total_repos = lang_agg.get("total", 0)
    analyzed_repos = conn.execute(
        "SELECT COUNT(*) AS c FROM repo_analysis WHERE category IS NOT NULL"
    ).fetchone()["c"]

    # category panorama: stored descriptions + top-3 per category via query_stars.
    cat_desc = {r["name"]: (r["description"] or "") for r in conn.execute(
        "SELECT name, description FROM categories").fetchall()}
    panorama = {}
    for b in cat_buckets:
        res = query_stars(conn, mode="list", category=[b["key"]],
                          sort="stars_desc", limit=3)
        panorama[b["key"]] = res.get("items", [])

    treasures = _treasures(conn, active_recent_days, now)
    archived, stale = _legacy(conn)
    verdicts = _legacy_verdicts(llm, archived, stale)

    # interest timeline: derive year -> category counts from one enriched list.
    timeline = _timeline(conn)

    # ideas (shared single LLM batch over the candidate pool).
    pool = _candidate_pool(conn)
    derivative, combos = _discover_ideas(llm, pool, max_combo_candidates)

    meta = {
        "total_repos": total_repos,
        "analyzed_repos": analyzed_repos,
        "snapshot_at": _snapshot_time(conn),
        "sections": {
            "languages": len(lang_buckets),
            "categories": len(cat_buckets),
            "treasures": len(treasures),
            "legacy_archived": len(archived),
            "legacy_stale": len(stale),
            "timeline_years": len(timeline),
        },
        "ideas": {
            "derivative": len(derivative),
            "combos": len(combos),
            "total": len(derivative) + len(combos),
        },
        "search_verified": search is not None,
        "llm_calls": getattr(llm, "call_count", 0),
    }

    report_md = _render_report(
        conn, meta, lang_buckets, cat_buckets, cat_desc, panorama,
        treasures, archived, stale, verdicts, timeline, active_recent_days)
    ideas_md = _render_ideas(derivative, combos, search)

    if write_to:
        import os
        os.makedirs(write_to, exist_ok=True)
        with open(os.path.join(write_to, "report.md"), "w", encoding="utf-8") as f:
            f.write(report_md)
        with open(os.path.join(write_to, "ideas.md"), "w", encoding="utf-8") as f:
            f.write(ideas_md)
        meta["written_to"] = write_to

    return {"report_markdown": report_md, "ideas_markdown": ideas_md,
            "meta": meta}


def _timeline(conn):
    """year -> [(category, count), ...] desc, from one enriched query_stars list."""
    res = query_stars(conn, mode="list", sort="starred_desc", limit=500)
    by_year = {}
    for it in res.get("items", []):
        sa = it.get("starred_at") or ""
        if len(sa) < 4 or not sa[:4].isdigit():
            continue
        year = sa[:4]
        counts = by_year.setdefault(year, {})
        cat = it.get("category") or "未分类"
        counts[cat] = counts.get(cat, 0) + 1
    return {y: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
            for y, c in by_year.items()}
