"""FastMCP server exposing sync_stars over stdio."""
from mcp.server.fastmcp import FastMCP

from . import config, db, llm
from .github import GitHubClient
from .query import query_stars as _query_stars
from .report import generate_report as _generate_report
from .search import build_search
from .sync import sync_stars as _sync_stars
from .tag import tag_repos as _tag_repos

mcp = FastMCP("github-star")


@mcp.tool()
def sync_stars(full: bool = False) -> dict:
    """Sync the current account's GitHub stars into local SQLite.

    Incremental by default (stops once it reaches already-synced stars).
    Set full=True to force a complete walk. Requires GITHUB_TOKEN.
    """
    token = config.github_token()  # raises before any request if missing
    conn = db.connect(config.db_path())
    try:
        client = GitHubClient(token)
        return _sync_stars(conn, client, full=full)
    finally:
        conn.close()


@mcp.tool()
def tag_repos(retag_stale: bool = False, batch_size: int = 20,
              limit: int | None = None) -> dict:
    """Tag synced repos with the LLM: free tags -> categories -> backfill.

    Only untagged (or, with retag_stale=True, stale-past-TAG_TTL_DAYS) repos
    reach the LLM — already-tagged fresh repos are skipped, so reruns cost
    zero LLM calls. Resumable: a killed run continues from its last phase.

    Requires GITHUB_TOKEN (README fetch) and LLM_API_KEY (+ optional
    LLM_BASE_URL / LLM_MODEL, default DeepSeek). batch_size sets the
    commit/checkpoint granularity; limit caps repos processed (testing).
    """
    token = config.github_token()   # raise before any work if missing
    llm_client = llm.build_client()  # raises if LLM_API_KEY missing
    conn = db.connect(config.db_path())
    try:
        client = GitHubClient(token)
        return _tag_repos(
            conn, client, llm_client,
            retag_stale=retag_stale, batch_size=batch_size, limit=limit,
            ttl_days=config.tag_ttl_days(),
            readme_max_chars=config.readme_max_chars(),
        )
    finally:
        conn.close()


@mcp.tool()
def query_stars(mode: str = "list", group_by: str | None = None,
                category: list[str] | None = None,
                audience: list[str] | None = None,
                niche: list[str] | None = None,
                status: list[str] | None = None,
                language: list[str] | None = None,
                starred_after: str | None = None,
                starred_before: str | None = None,
                min_stars: int | None = None, max_stars: int | None = None,
                sort: str = "stars_desc", limit: int = 50,
                offset: int = 0) -> dict:
    """Query synced stars along five dimensions; no GitHub/LLM call needed.

    Dimensions: 功能用途 (category), 面向用户 (audience: 开发者/设计师/终端用户/
    运维/研究者/未知), 小众偏门 (niche: 极小众/小众/成熟/主流), 项目状态
    (status: 活跃/停更/archived), star 时间 (starred_after/before, YYYY or
    YYYY-MM-DD). All filters are optional, combine with AND, and a list-valued
    filter is OR within itself.

    mode="list" returns a paginated repo list (limit/offset, max limit 500);
    mode="aggregate" needs group_by (category|audience|niche|status|year|
    language) and returns count-per-bucket. Category names are LLM-generated —
    call aggregate(group_by="category") first to discover them, then filter.
    Illegal mode/group_by/enum values return a structured error, not an
    exception. Requires a synced (and ideally tagged) DB; empty DB → total 0.
    """
    conn = db.connect(config.db_path())
    try:
        return _query_stars(
            conn, mode=mode, group_by=group_by, category=category,
            audience=audience, niche=niche, status=status, language=language,
            starred_after=starred_after, starred_before=starred_before,
            min_stars=min_stars, max_stars=max_stars, sort=sort,
            limit=limit, offset=offset)
    finally:
        conn.close()


@mcp.tool()
def generate_report(write_to: str | None = None) -> dict:
    """Produce the Star analysis report + idea list from the local DB in one call.

    Reads the tagged library (requires a prior sync_stars + tag_repos) and
    returns a dict with `report_markdown` (5 sections: 总览 / 功能类目全景 /
    小众宝藏 Top20 / 遗产项目 / 兴趣时间线), `ideas_markdown` (≥10 ideas,
    衍生型 + 组合型; every repo reference is validated to exist in the library),
    and `meta` (counts + snapshot time).

    Combo ideas carry a demand-verification field: server-side evidence when
    SEARCH_PROVIDER + SEARCH_API_KEY are set, otherwise a 未验证 note handing
    verification to the client dialogue. Requires LLM_API_KEY for idea
    discovery. If `write_to` is a directory path, also writes report.md and
    ideas.md there.
    """
    llm_client = llm.build_client()  # raises if LLM_API_KEY missing
    search = (build_search(config.search_provider(), config.search_api_key())
              if config.search_configured() else None)
    conn = db.connect(config.db_path())
    try:
        return _generate_report(
            conn, llm_client, search=search, write_to=write_to,
            active_recent_days=config.active_recent_days(),
            max_combo_candidates=config.max_combo_candidates(),
        )
    finally:
        conn.close()


def main() -> None:
    mcp.run()  # transport=stdio by default


if __name__ == "__main__":
    main()
