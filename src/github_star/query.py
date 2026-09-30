"""query_stars: five-dimension filter + aggregate over the local star DB.

Supports conversational exploration of stars along five dimensions:

  功能用途 category   — repo_analysis.category (LLM backfill), NULL → 未分类
  面向用户 audience   — derived at query time from repo_analysis.audience text
  小众偏门 niche      — computed at query time from repos.stargazers_count
  项目状态 status     — computed at query time from is_archived + pushed_at
  star 时间 starred_at — repos.starred_at, filterable / groupable by year

Design decisions carried over from the M3 spec (MYS-49):

* niche & status are computed **at query time** from the raw `repos` columns,
  never read from M2's `niche_bucket` / `status_class` snapshots — the snapshot
  of "停更" goes stale as time passes, and repos M2 skipped have no snapshot at
  all. The thresholds reuse the exact M2 functions (`tag._niche_bucket` /
  `tag._status_class`) so the two milestones can never drift apart.
* audience is normalized at query time from M2's free-text `audience` line by
  deterministic keyword rules — M2 stores prose, not the target enum, and we do
  not reach back to re-tag M2.
* a LEFT JOIN keeps repos M2 never backfilled: they surface as category=未分类,
  audience=未知, while niche/status still compute from the raw columns.

Illegal enum / mode / group_by values return a structured `{"error": ...}`
dict — never a silent empty result or an uncaught exception.
"""
import json

from .tag import _niche_bucket, _status_class

# --- enum vocabularies (single source of truth for validation) --------------

MODES = ("list", "aggregate")
GROUP_BYS = ("category", "audience", "niche", "status", "year", "language")
SORTS = ("stars_desc", "stars_asc", "starred_desc", "starred_asc")

NICHE_VALUES = ("极小众", "小众", "成熟", "主流")
STATUS_VALUES = ("活跃", "停更", "archived")
AUDIENCE_VALUES = ("开发者", "设计师", "终端用户", "运维", "研究者", "未知")

UNCATEGORIZED = "未分类"
UNKNOWN_AUDIENCE = "未知"
UNKNOWN_LANGUAGE = "未知"

LIMIT_MAX = 500

# audience keyword rules, checked in order — first hit wins (§二). Keys are the
# target enum; each maps to the substrings that route to it (case-insensitive).
_AUDIENCE_RULES = (
    ("开发者", ("开发", "developer", "程序员", "工程师")),
    ("设计师", ("设计", "design", "ui", "ux")),
    ("运维", ("运维", "ops", "devops", "sre", "部署")),
    ("研究者", ("研究", "科研", "research", "学术", "论文")),
)


def _err(message: str) -> dict:
    return {"error": message}


def _audience_of(text: str | None) -> str:
    """Normalize M2's free-text audience into the target enum (§二).

    Deterministic, keyword-based; noisy by design (free text quality varies) but
    good enough for conversational exploration. NULL → 未知.
    """
    if not text:
        return UNKNOWN_AUDIENCE
    low = text.lower()
    for label, keywords in _AUDIENCE_RULES:
        if any(k in low for k in keywords):
            return label
    return "终端用户"


def _status_of(is_archived, pushed_at: str | None) -> str:
    """Query-time maintenance status.

    Reuses M2's exact archived / >365-day logic, but treats a missing push time
    conservatively as 停更 (§二) — M2 defaults an absent push time to 活跃, which
    is fine at tag time but wrong for a repo that simply never reported one.
    """
    if not is_archived and not pushed_at:
        return "停更"
    return _status_class(is_archived, pushed_at)


def _year_of(starred_at: str | None) -> str | None:
    """Year prefix of an ISO8601 starred_at (e.g. '2023'); None if unparseable."""
    if not starred_at or len(starred_at) < 4 or not starred_at[:4].isdigit():
        return None
    return starred_at[:4]


def _enrich(row) -> dict:
    """Turn a joined DB row into an item with all five derived dimensions."""
    try:
        topics = json.loads(row["topics"] or "[]")
    except (ValueError, TypeError):
        topics = []
    item = {
        "full_name": row["full_name"],
        "description": row["description"],
        "language": row["language"],
        "stargazers_count": row["stargazers_count"],
        "html_url": row["html_url"],
        "topics": topics,
        "category": row["a_category"] or UNCATEGORIZED,
        "audience": _audience_of(row["a_audience"]),
        "niche": _niche_bucket(row["stargazers_count"]),
        "status": _status_of(row["is_archived"], row["pushed_at"]),
        "starred_at": row["starred_at"],
        "pushed_at": row["pushed_at"],
    }
    if row["a_summary"]:
        item["summary"] = row["a_summary"]
    return item


def _passes(item: dict, category, audience, niche, status, language,
            starred_after, starred_before, min_stars, max_stars) -> bool:
    """AND across dimensions; OR within a multi-value list filter (§二)."""
    if category and item["category"] not in category:
        return False
    if audience and item["audience"] not in audience:
        return False
    if niche and item["niche"] not in niche:
        return False
    if status and item["status"] not in status:
        return False
    if language and (item["language"] or UNKNOWN_LANGUAGE) not in language:
        return False
    stars = item["stargazers_count"] or 0
    if min_stars is not None and stars < min_stars:
        return False
    if max_stars is not None and stars > max_stars:
        return False
    # ISO8601 sorts lexically; a "YYYY" bound compares as a prefix (§五).
    sa = item["starred_at"] or ""
    if starred_after and sa < starred_after:
        return False
    if starred_before and sa > starred_before:
        return False
    return True


_SORTS = {
    "stars_desc": (lambda i: i["stargazers_count"] or 0, True),
    "stars_asc": (lambda i: i["stargazers_count"] or 0, False),
    "starred_desc": (lambda i: i["starred_at"] or "", True),
    "starred_asc": (lambda i: i["starred_at"] or "", False),
}

_GROUP_KEY = {
    "category": lambda i: i["category"],
    "audience": lambda i: i["audience"],
    "niche": lambda i: i["niche"],
    "status": lambda i: i["status"],
    "language": lambda i: i["language"] or UNKNOWN_LANGUAGE,
    "year": lambda i: _year_of(i["starred_at"]) or UNKNOWN_LANGUAGE,
}


def _validate_enums(name, values, allowed) -> dict | None:
    if values is None:
        return None
    bad = [v for v in values if v not in allowed]
    if bad:
        return _err(f"非法 {name} 值: {bad}；可选: {list(allowed)}")
    return None


def query_stars(conn, mode="list", group_by=None,
                category=None, audience=None, niche=None, status=None,
                language=None, starred_after=None, starred_before=None,
                min_stars=None, max_stars=None, sort="stars_desc",
                limit=50, offset=0) -> dict:
    """Filter + aggregate stars along five dimensions. See module docstring.

    All filters are optional and combine with AND; a list-valued filter is OR
    within itself. Returns a list page or an aggregate count, or a structured
    `{"error": ...}` dict for any illegal mode / group_by / enum value.
    """
    if mode not in MODES:
        return _err(f"非法 mode: {mode!r}；可选: {list(MODES)}")
    if sort not in SORTS:
        return _err(f"非法 sort: {sort!r}；可选: {list(SORTS)}")
    for name, values, allowed in (
        ("niche", niche, NICHE_VALUES),
        ("status", status, STATUS_VALUES),
        ("audience", audience, AUDIENCE_VALUES),
    ):
        error = _validate_enums(name, values, allowed)
        if error:
            return error
    if mode == "aggregate":
        if group_by is None:
            return _err("aggregate 模式必须指定 group_by")
        if group_by not in GROUP_BYS:
            return _err(f"非法 group_by: {group_by!r}；可选: {list(GROUP_BYS)}")

    rows = conn.execute(
        """
        SELECT r.full_name, r.description, r.language, r.stargazers_count,
               r.html_url, r.topics, r.is_archived, r.pushed_at, r.starred_at,
               a.category AS a_category, a.audience AS a_audience,
               a.summary  AS a_summary
        FROM repos r
        LEFT JOIN repo_analysis a ON a.repo_id = r.id
        """
    ).fetchall()

    items = [
        item for item in (_enrich(r) for r in rows)
        if _passes(item, category, audience, niche, status, language,
                   starred_after, starred_before, min_stars, max_stars)
    ]
    total = len(items)

    if mode == "aggregate":
        counts: dict[str, int] = {}
        key_of = _GROUP_KEY[group_by]
        for item in items:
            k = key_of(item)
            counts[k] = counts.get(k, 0) + 1
        # count desc, then key asc for a stable, readable order
        buckets = [{"key": k, "count": c}
                   for k, c in sorted(counts.items(),
                                      key=lambda kv: (-kv[1], kv[0]))]
        return {"mode": "aggregate", "group_by": group_by,
                "total": total, "buckets": buckets}

    # list mode: sort, then clamp + paginate
    key_fn, reverse = _SORTS[sort]
    items.sort(key=key_fn, reverse=reverse)
    limit = max(0, min(int(limit), LIMIT_MAX))
    offset = max(0, int(offset))
    page = items[offset:offset + limit]
    return {"mode": "list", "total": total, "count": len(page),
            "limit": limit, "offset": offset, "items": page}
