# github-star-inference

An MCP server that syncs your GitHub stars into a local SQLite database and (in
later milestones) tags and analyzes them. This is **M1**: the project skeleton,
data layer, and the `sync_stars` tool.

## Requirements

- Python >= 3.10
- [uv](https://docs.astral.sh/uv/) for dependency management

## Setup

```bash
uv sync
cp .env.example .env   # then fill in GITHUB_TOKEN
```

### GitHub token

`sync_stars` reads the current account's stars via the GitHub REST API, so it
needs a Personal Access Token in `GITHUB_TOKEN`:

1. Go to GitHub → Settings → Developer settings → Personal access tokens.
2. Create a token (classic or fine-grained). Read access is enough — the
   `public_repo` scope (classic) or public-repository read (fine-grained)
   covers listing your stars. Add private-repo read only if you star private
   repos and want them included.
3. Put it in `.env` as `GITHUB_TOKEN=...` (never commit `.env`).

## Run as an MCP server (stdio)

```bash
uv run github-star
```

Register it with an MCP client (e.g. Claude Desktop) — replace `/abs/path`
with this repo's absolute path:

```json
{
  "mcpServers": {
    "github-star": {
      "command": "uv",
      "args": ["run", "--directory", "/abs/path", "github-star"],
      "env": { "GITHUB_TOKEN": "ghp_xxx" }
    }
  }
}
```

## The `sync_stars` tool

```
sync_stars(full: bool = False) -> dict
```

Pulls your stars into the local SQLite file (`github_star.db` by default;
override with `GITHUB_STAR_DB`), deduping by GitHub repo id.

- **Incremental (default):** results come back newest-star-first, so the DB
  holds the newest prefix. Once `sync_stars` reaches a page it has fully seen
  before, it stops — a rerun after starring one new repo only fetches the
  first page or two.
- **First run / resume:** an empty DB, or a DB whose last sync was interrupted,
  walks every page. Each page is committed as it lands, and inserts use
  `INSERT OR IGNORE`, so a killed sync resumes cleanly on the next run with no
  duplicates.
- **`full=True`:** forces a complete walk (no early stop), for a manual
  full recalibration.

It returns a summary: `synced` (new rows), `total_in_db`, `mode`, and
timestamps.

### Rate limits & errors (no silent data loss)

- Honors `x-ratelimit-remaining` / `x-ratelimit-reset`; sleeps until reset when
  the quota is exhausted.
- On `403`/`429`, waits per `retry-after`, else until reset, else exponential
  backoff with jitter (capped, max 5 retries).
- Transient network errors are retried with backoff; if retries are exhausted,
  already-fetched pages stay committed and the sync exits with its progress —
  the next run resumes.
- Missing `GITHUB_TOKEN` fails **before** any request. An invalid token (401)
  fails immediately and is **not** retried.
- Requests are sequential (no concurrency) to avoid secondary rate limits.

## Known limitations (M1)

- **Renamed/transferred repos:** dedup is by GitHub repo id (stable across
  renames), so a transferred repo is not re-inserted — but its stored
  `full_name` is **not** refreshed. Metadata refresh is deferred to a later
  milestone.
- README/native description is not fetched; the `readme` column is reserved and
  left empty for M2.
- Existing repos' metadata changes are not refreshed — only new stars are added.
- Single account only (one `GITHUB_TOKEN`); REST only, no GraphQL.

## Development

```bash
uv run pytest
```

Tests use mocked HTTP responses and never hit the real API.

## Roadmap

- **M1 (this):** skeleton + SQLite + `sync_stars`.
- **M2:** LLM tagging (`tag_repos`) — fills `readme`, `repo_tags`,
  `repo_analysis`.
- **M3:** querying (`query_stars`).
- **M4:** reporting (`generate_report`).
