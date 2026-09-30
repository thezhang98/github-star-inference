"""FastMCP server exposing sync_stars over stdio."""
from mcp.server.fastmcp import FastMCP

from . import config, db
from .github import GitHubClient
from .sync import sync_stars as _sync_stars

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


def main() -> None:
    mcp.run()  # transport=stdio by default


if __name__ == "__main__":
    main()
