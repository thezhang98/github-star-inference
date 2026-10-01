# github-star-inference

An MCP server that syncs your GitHub stars into a local SQLite database and
tags/analyzes them with an LLM. **M1** added the skeleton, data layer, and
`sync_stars`; **M2** adds `tag_repos` — LLM free-tagging, category convergence,
and backfill; **M3** adds `query_stars` — five-dimension filter/aggregate; **M4**
adds `generate_report` — the Star analysis report + idea list.

## Requirements

- Python >= 3.10
- SQLite >= 3.24 (the metadata layer uses `INSERT ... ON CONFLICT DO UPDATE`
  UPSERT, added in SQLite 3.24 / 2018-06). Most Python 3.10+ builds ship a
  newer SQLite; on an older system library, upgrade it or install `pysqlite3`.
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

## The `tag_repos` tool (M2)

```
tag_repos(retag_stale: bool = False, batch_size: int = 20, limit: int | None = None) -> dict
```

Tags the repos `sync_stars` collected, in four resumable phases:

1. **Fetch README** for repos missing one (truncated to `README_MAX_CHARS`,
   default 4000; a repo with no README is stored as empty so it isn't refetched).
2. **Free-tag (round 1):** the LLM reads `full_name` + description + topics +
   language + README and returns 3–5 free tags, an audience line, and a summary.
   These land in `repo_tags(round=1)` and `repo_analysis`.
3. **Cluster (pure LLM):** all round-1 tags + frequencies are converged into
   20–40 categories (`categories` table). If the model returns a count outside
   20–40 it's reprompted up to twice; a persistent out-of-band result is
   accepted with a logged warning (hard bounds 15/50 stop the loop).
4. **Backfill:** each tagged repo is assigned one category, plus a
   script-computed `niche_bucket` (by stars) and `status_class` (archived /
   停更 / 活跃), and `analyzed_at` is stamped.

### Zero repeat calls & staleness

Only untagged repos reach the LLM. A rerun with nothing new makes **zero LLM
calls** — the returned `llm_calls` counter (also stored in
`meta.last_tag_llm_calls`) makes this assertable. `retag_stale=True` also
re-tags repos whose `analyzed_at` is older than `TAG_TTL_DAYS` (default 90),
deleting their old round-1 tags first so nothing stale lingers.

### Resumable & failure-isolated

Each batch (`batch_size`) is committed, and the current phase is tracked in
`meta.tag_phase`, so a killed run resumes without duplicate calls or rows. If
one repo's LLM call fails all retries, that repo is **skipped and left
untagged** (retried on the next run) — the batch never aborts and no partial
row is written.

### Configuration

Needs `GITHUB_TOKEN` (README fetch) and an OpenAI-compatible LLM: `LLM_API_KEY`
(required), `LLM_BASE_URL` (default `https://api.deepseek.com`), `LLM_MODEL`
(default `deepseek-chat`). Optional: `TAG_TTL_DAYS`, `README_MAX_CHARS`,
`USER_FOCUS_AREAS` (comma-separated hint that nudges category convergence).
See `.env.example`. DeepSeek keys: https://platform.deepseek.com/.

> Offline tests mock both GitHub and the LLM. Real end-to-end tagging against a
> live LLM endpoint is pending verification in a key-bearing environment.

## The `generate_report` tool (M4)

```
generate_report(write_to: str | None = None) -> dict
```

One call reads the tagged library and returns a dict with three keys:

- `report_markdown` — the **Star 数据分析报告**, 5 sections:
  1. 总览 (star total, analyzed count, language distribution, snapshot time)
  2. 功能用途细分类目全景 (each category + its top repos)
  3. 小众宝藏 Top 20 — low-star but genuinely alive gems
  4. 遗产项目 — archived vs 停更, listed separately, each tagged 值得复活 /
     有现代替代品 / 纯遗产
  5. 兴趣演变时间线 — category mix per star-year
- `ideas_markdown` — the **想法清单** (≥10 when the library supports it), two kinds:
  - 衍生型: direction + in-library data support + type + feasibility
  - 组合型: the above + the A/B source repos + a demand-verification line
- `meta` — repo totals, snapshot time, and per-section / per-idea counts.

Every repo the report or ideas reference is validated to exist in the local
DB — the LLM may only cite repos by id, and any out-of-library reference is
dropped, so nothing is fabricated. The report reuses `query_stars` and M2's
stored `niche_bucket` / `status_class`; it never recomputes them or builds a
second query path.

`write_to=<dir>` also writes `report.md` and `ideas.md` into that directory.

### Demand verification (two channels)

Combo ideas need their real-world demand checked. If `SEARCH_PROVIDER`
(`tavily` | `brave`) **and** `SEARCH_API_KEY` are both set, the server verifies
each combo via that search API and fills in evidence URLs + a conclusion. If
either is unset, combo ideas carry a 未验证 note handing verification to the MCP
client dialogue instead. A configured search that errors degrades to 未验证
(with a `(搜索请求失败)` suffix) rather than aborting the report.

### Configuration

Needs `LLM_API_KEY` (idea discovery). Optional M4 knobs:
`ACTIVE_RECENT_DAYS` (小众宝藏 recency window, default 180),
`MAX_COMBO_CANDIDATES` (max combo pairs per LLM call, default 15),
`SEARCH_PROVIDER` + `SEARCH_API_KEY` (demand verification, see above).
Run `sync_stars` then `tag_repos` first — `generate_report` errors on an
empty/untagged DB rather than emitting a shell report.

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
