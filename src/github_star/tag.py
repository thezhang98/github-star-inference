"""tag_repos pipeline: README fetch -> free tagging -> LLM clustering -> backfill.

Four phases, each idempotent and resumable from the `tag_phase` meta key:

  free_tagging  round-1: 3–5 free tags + audience + summary per repo (LLM)
  clustering    converge free tags into 20–40 categories (pure LLM)
  backfilling   assign each repo to one category (LLM) + script-computed
                niche_bucket / status_class; sets analyzed_at
  completed

Failure isolation (§五3): a repo whose LLM call fails after retries is skipped
and left untagged — never a partial write — so the next run retries it. The
run never aborts the whole batch for one bad repo.

Per-batch commits make a killed run resume without duplicate LLM calls or rows.
"""
import json
import logging
from datetime import datetime, timezone

from . import db
from .llm import LLMError

log = logging.getLogger("github_star.tag")

# clustering count guardrails (§五5)
TARGET_MIN, TARGET_MAX = 20, 40   # desired category count
HARD_MIN, HARD_MAX = 15, 50       # absolute bounds that stop the reprompt loop
CLUSTER_REPROMPTS = 2             # extra attempts to land inside the target band

# Only the most-frequent tags are clustered. Free-tagging yields a very long,
# noisy tail — a few hundred repos produce ~1000+ distinct tags, nearly all
# appearing once — and feeding the whole list makes the model loop/degenerate
# and overflow its JSON response (observed as unterminated JSON on real data).
# The top slice converges cleanly into 20–40 categories; rare one-off tags are
# still covered because backfill assigns every repo to the best resulting
# category via its own call, regardless of whether its tags were in this sample.
CLUSTER_MAX_TAGS = 250

_FREE_SYSTEM = (
    "你是开源项目分类助手。阅读一个 GitHub 仓库的元信息，输出严格的 JSON："
    '{"tags": [3-5个中文自由标签], "audience": "面向用户/受众一句话", '
    '"summary": "基于介绍/README 的一句话中文摘要"}。'
    "标签要具体（技术栈/领域/用途），不要空泛词。只输出 JSON。"
)

_CLUSTER_SYSTEM = (
    "你是标签聚类助手。给定一批自由标签及其词频，把语义相近的标签归并成"
    f" {TARGET_MIN}-{TARGET_MAX} 个细分类目。输出严格 JSON："
    '{"categories": [{"name": "类目名", "description": "一句话点评该类反映的兴趣", '
    '"member_tags": ["归并进来的原始标签", ...]}, ...]}。'
    "类目名简洁可读、互不重叠，覆盖全部标签。只输出 JSON。"
)

_BACKFILL_SYSTEM = (
    "你是归类助手。给定一个仓库的自由标签和一份类目表，从类目表里选出最贴切的"
    '唯一一个类目。输出严格 JSON：{"category": "类目名"}。category 必须来自给定'
    "类目表。只输出 JSON。"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _niche_bucket(stars: int | None) -> str:
    """Pure-script popularity bucket by star count (§三)."""
    s = stars or 0
    if s < 100:
        return "极小众"
    if s < 1000:
        return "小众"
    if s < 10000:
        return "成熟"
    return "主流"


def _status_class(is_archived, pushed_at: str | None) -> str:
    """Pure-script maintenance status: archived / 停更 / 活跃 (§三)."""
    if is_archived:
        return "archived"
    if pushed_at:
        try:
            pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - pushed).days > 365:
                return "停更"
        except ValueError:
            pass
    return "活跃"


def _repo_prompt(repo, readme: str | None, max_chars: int) -> str:
    topics = ", ".join(json.loads(repo["topics"] or "[]"))
    parts = [
        f"仓库: {repo['full_name']}",
        f"语言: {repo['language'] or '未知'}",
        f"topics: {topics or '无'}",
        f"介绍: {repo['description'] or '无'}",
    ]
    if readme:
        parts.append(f"README(截断): {readme[:max_chars]}")
    return "\n".join(parts)


def _phase_fetch_readme(conn, client, repos, max_chars, batch_size):
    """Fetch + truncate + store READMEs for repos that lack one."""
    done = 0
    for repo in repos:
        try:
            text = client.get_readme(repo["owner"], repo["name"])
        except Exception as exc:  # noqa: BLE001 — isolate per repo, retry next run
            log.warning("readme fetch failed for %s: %s", repo["full_name"], exc)
            continue
        # store empty string (not NULL) so a README-less repo isn't re-fetched
        db.set_readme(conn, repo["id"], (text or "")[:max_chars])
        done += 1
        if done % batch_size == 0:
            conn.commit()
    conn.commit()
    return done


def _phase_free_tag(conn, llm, repos, max_chars, retag_stale, batch_size):
    """Round-1 free tagging. Skipped repos stay untagged (failure isolation)."""
    tagged = skipped = 0
    for repo in repos:
        prompt = _repo_prompt(repo, repo["readme"], max_chars)
        try:
            out = llm.chat_json(_FREE_SYSTEM, prompt)
        except LLMError as exc:
            log.warning("free-tag skip %s: %s", repo["full_name"], exc)
            skipped += 1
            continue

        tags = [str(t).strip() for t in (out.get("tags") or []) if str(t).strip()]
        if not tags:
            tags = ["uncategorized"]  # empty-info fallback (§五1)
        tags = tags[:5]
        now = _now()
        analysis = {
            "summary": (out.get("summary") or "").strip() or None,
            "audience": (out.get("audience") or "").strip() or None,
            "model": llm.model,
        }
        if retag_stale:
            # clear old round-1 tags so stale labels don't linger (§澄清①), and
            # reset category+analyzed_at so this repo re-enters backfill and gets
            # a fresh analyzed_at. Without this, a stale repo is re-tagged but
            # never re-stamped → judged stale forever → infinite LLM calls (bug①).
            db.clear_round1_tags(conn, repo["id"])
            analysis["category"] = None
            analysis["analyzed_at"] = None
        db.add_round1_tags(conn, repo["id"], tags, llm.model, now)
        db.upsert_analysis(conn, repo["id"], analysis)
        tagged += 1
        if tagged % batch_size == 0:
            conn.commit()
    conn.commit()
    return tagged, skipped


def _phase_cluster(conn, llm):
    """Converge round-1 tags into TARGET_MIN..TARGET_MAX categories (§五5)."""
    freq = db.round1_tag_frequency(conn)
    if not freq:
        return 0
    # cluster the most-frequent tags only; the long one-off tail is left to
    # backfill (see CLUSTER_MAX_TAGS). freq is already count-desc.
    freq = freq[:CLUSTER_MAX_TAGS]
    lines = "\n".join(f"{tag} ({n})" for tag, n in freq)
    from . import config
    focus = config.user_focus_areas()
    base = f"自由标签及词频：\n{lines}"
    if focus:
        base += f"\n\n用户关注领域（聚类时适当倾斜）：{focus}"

    user = base
    best = None
    for attempt in range(CLUSTER_REPROMPTS + 1):
        out = llm.chat_json(_CLUSTER_SYSTEM, user)
        cats = [c for c in (out.get("categories") or []) if c.get("name")]
        best = cats
        n = len(cats)
        if TARGET_MIN <= n <= TARGET_MAX:
            break
        if attempt < CLUSTER_REPROMPTS and HARD_MIN <= n <= HARD_MAX:
            action = "请合并到" if n > TARGET_MAX else "请拆分到"
            user = (f"{base}\n\n上一版产出了 {n} 个类目，超出目标区间"
                    f" {TARGET_MIN}-{TARGET_MAX}，{action} {TARGET_MIN}-{TARGET_MAX}"
                    " 个之间，覆盖全部标签。")
        else:
            break

    n = len(best or [])
    if not (TARGET_MIN <= n <= TARGET_MAX):
        log.warning("cluster count %d outside target %d-%d; accepting best",
                    n, TARGET_MIN, TARGET_MAX)
    db.replace_categories(conn, best or [], _now())
    conn.commit()
    return db.category_count(conn)


def _phase_backfill(conn, llm, repos, batch_size):
    """Assign each repo to one category + script-computed buckets (§三)."""
    names = db.category_names(conn)
    catalog = "、".join(names)
    filled = skipped = 0
    for repo in repos:
        tags = db.round1_tags_for(conn, repo["id"])
        prompt = (f"仓库标签：{', '.join(tags)}\n\n可选类目表：{catalog}\n\n"
                  "选出最贴切的唯一类目。")
        try:
            out = llm.chat_json(_BACKFILL_SYSTEM, prompt)
        except LLMError as exc:
            log.warning("backfill skip %s: %s", repo["full_name"], exc)
            skipped += 1
            continue
        category = (out.get("category") or "").strip()
        if category not in names:
            # LLM strayed off the catalog — snap to first tag's nearest or first
            category = names[0] if names else "uncategorized"
        db.upsert_analysis(conn, repo["id"], {
            "category": category,
            "niche_bucket": _niche_bucket(repo["stargazers_count"]),
            "status_class": _status_class(repo["is_archived"], repo["pushed_at"]),
            "model": llm.model,
            "analyzed_at": _now(),
        })
        filled += 1
        if filled % batch_size == 0:
            conn.commit()
    conn.commit()
    return filled, skipped


def tag_repos(conn, client, llm, retag_stale: bool = False,
              batch_size: int = 20, limit: int | None = None,
              ttl_days: int = 90, readme_max_chars: int = 4000) -> dict:
    """Run the full tagging pipeline, resumable from `tag_phase`.

    `client` fetches READMEs; `llm` does tagging/clustering/backfill. Both are
    injected so tests run fully offline. Returns per-phase counters.
    """
    stats = {"tagged": 0, "tag_skipped": 0, "categories": 0,
             "backfilled": 0, "backfill_skipped": 0, "readmes": 0}

    # phase 1: README fetch
    db.set_meta(conn, "tag_phase", "free_tagging")
    conn.commit()
    stats["readmes"] = _phase_fetch_readme(
        conn, client,
        db.repos_missing_readme(conn, retag_stale, ttl_days),
        readme_max_chars, batch_size)

    # phase 2: round-1 free tagging (already-tagged, non-stale repos excluded)
    to_tag = db.repos_to_tag(conn, retag_stale, ttl_days, limit)
    stats["tagged"], stats["tag_skipped"] = _phase_free_tag(
        conn, llm, to_tag, readme_max_chars, retag_stale, batch_size)

    # phase 3: clustering — ONLY when no categories exist yet (first run /
    # resume-after-kill). Incremental/re-tag runs deliberately do NOT re-cluster:
    # rebuilding the category set would rename categories and leave already-
    # backfilled repos pointing at deleted names (dangling category, bug②).
    # New/re-tagged repos are instead backfilled against the EXISTING catalog.
    # (Rebuilding the whole taxonomy once enough new repos accrue is out of M2
    # scope — see known limitations; deferred to a future milestone.)
    if db.category_count(conn) == 0:
        db.set_meta(conn, "tag_phase", "clustering")
        conn.commit()
        stats["categories"] = _phase_cluster(conn, llm)
    else:
        stats["categories"] = db.category_count(conn)

    # phase 4: backfill
    db.set_meta(conn, "tag_phase", "backfilling")
    conn.commit()
    stats["backfilled"], stats["backfill_skipped"] = _phase_backfill(
        conn, llm, db.repos_needing_backfill(conn), batch_size)

    db.set_meta(conn, "tag_phase", "completed")
    db.set_meta(conn, "last_tag_completed_at", _now())
    db.set_meta(conn, "last_tag_llm_calls", llm.call_count)
    conn.commit()

    stats["llm_calls"] = llm.call_count
    stats["phase"] = "completed"
    return stats
