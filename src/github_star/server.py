"""FastMCP server exposing sync_stars over stdio."""
from mcp.server.fastmcp import FastMCP

from . import config, db, llm
from .github import GitHubClient
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


def main() -> None:
    mcp.run()  # transport=stdio by default


if __name__ == "__main__":
    main()
