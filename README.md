# github-star-inference

把你的 GitHub star 整理成可对话、可分析的本地知识库。它是一个 MCP server，用四段流水线
`sync_stars → tag_repos → query_stars → generate_report` 把 star 同步进本地 SQLite、用 LLM
打标分类，再按功能用途 / 面向用户 / 小众程度 / 项目状态 / star 时间五个维度查询，最后产出一份
《Star 数据分析报告》和一份《想法清单》。

本地 SQLite 单文件（`github_star.db`）是唯一数据源，所有数字都来自库内真实数据，不编造。

## 架构概览

```
                         ┌──────────────┐
  GitHub REST API ──────▶│ sync_stars   │──写──▶ repos
                         └──────────────┘
                         ┌──────────────┐  读 repos / 写 repo_tags
  LLM (OpenAI 兼容) ─────▶│ tag_repos    │─────▶ categories / repo_analysis
  + GitHub README         └──────────────┘
                         ┌──────────────┐
            （无外部调用）│ query_stars  │──读──▶ repos + repo_analysis
                         └──────────────┘
                         ┌──────────────┐  读全部表 + LLM 产想法
  LLM (+ 可选搜索 API) ──▶│generate_report│────▶ report.md / ideas.md
                         └──────────────┘
```

四个 tool 对各表的读写：

| Tool | 读 | 写 | 外部依赖 |
|------|----|----|----------|
| `sync_stars` | — | `repos` | GitHub API |
| `tag_repos` | `repos` | `repos.readme`、`repo_tags`、`categories`、`repo_analysis` | GitHub（取 README）+ LLM |
| `query_stars` | `repos`、`repo_analysis` | — | 无 |
| `generate_report` | `repos`、`repo_analysis`、`categories` | 可选写出 `report.md` / `ideas.md` | LLM（+ 可选搜索 API） |

数据表：`repos`（star 原始元信息）、`repo_tags`（round-1 自由标签）、`categories`（聚类出的
20–40 个类目）、`repo_analysis`（每仓库的类目 / 受众 / 小众桶 / 状态 / 摘要）。

## 环境要求

- Python ≥ 3.10
- SQLite ≥ 3.24（元信息层用 `INSERT ... ON CONFLICT DO UPDATE` UPSERT，SQLite 3.24 / 2018-06
  起支持。多数 Python 3.10+ 自带的 SQLite 已够新；系统库过旧则升级它或装 `pysqlite3`）
- [uv](https://docs.astral.sh/uv/) 管理依赖

## 安装

```bash
uv sync
cp .env.example .env    # 然后按下节填好 .env
```

## 配置指南

所有配置都走环境变量。MCP 客户端通过它的 `env` 配置块注入（见下文挂接示例）；用脚本跑
runbook 时，`scripts/run_pipeline.py` 会自动读取当前目录的 `.env`。

| 变量 | 作用 | 是否必需 | 默认值 | 获取 / 说明 |
|------|------|----------|--------|-------------|
| `GITHUB_TOKEN` | 列出你的 star、取 README | **必需**（sync/tag 都用） | 无 | 见下方申请步骤 |
| `LLM_API_KEY` | LLM 打标 / 产报告想法 | **tag / report 必需** | 无 | DeepSeek: https://platform.deepseek.com/ |
| `LLM_BASE_URL` | OpenAI 兼容端点 | 否 | `https://api.deepseek.com` | 可指向任意 OpenAI 兼容服务（OpenAI、本地 vLLM/Ollama 网关等） |
| `LLM_MODEL` | 对话模型 id | 否 | `deepseek-chat` | 换端点时一并改 |
| `GITHUB_STAR_DB` | SQLite 文件路径 | 否 | `github_star.db` | 想换库位置 / 跑测试时用 |
| `TAG_TTL_DAYS` | 打标过期天数（`retag_stale` 用） | 否 | `90` | 老仓库超过该天数视为过期可重打 |
| `README_MAX_CHARS` | README 截断上限（存储 / 送 LLM 前） | 否 | `4000` | — |
| `USER_FOCUS_AREAS` | 聚类时倾斜的关注领域（逗号分隔） | 否 | 空 | 填了会让类目收敛偏向你的兴趣 |
| `ACTIVE_RECENT_DAYS` | 「小众宝藏」近期活跃窗口（天） | 否 | `180` | 比状态判定的 365 天更紧，确保宝藏真的活着 |
| `MAX_COMBO_CANDIDATES` | 一次报告里 LLM 产的组合型想法上限 | 否 | `15` | — |
| `SEARCH_PROVIDER` | 组合想法的需求验证后端 | 否 | 空 | `tavily` \| `brave`；见「需求验证」 |
| `SEARCH_API_KEY` | 搜索后端的 key | 否 | 空 | 与 `SEARCH_PROVIDER` 必须同时设，缺一即关闭 |

### GitHub token 申请步骤

`sync_stars` 用 GitHub REST API 读你当前账号的 star，需要一个放在 `GITHUB_TOKEN` 的 PAT：

1. GitHub → Settings → Developer settings → Personal access tokens。
2. 新建 token（classic 或 fine-grained）。只读即可——classic 勾 `public_repo`，或
   fine-grained 给 public repository 读权限，就够列出 star。只有你 star 了私有仓库并想纳入时，
   才额外加私有仓库读权限。
3. 复制后填进 `.env` 的 `GITHUB_TOKEN=...`（`.env` 不要提交）。

### LLM key

默认走 DeepSeek：在 https://platform.deepseek.com/ 申请 key 填进 `LLM_API_KEY` 即可，
`LLM_BASE_URL` / `LLM_MODEL` 用默认值。要换别的 OpenAI 兼容服务，把三件套一起改掉。

### 需求验证（可选）

`generate_report` 产的组合型想法默认带「未验证」标注，交给你在 MCP 客户端对话里联网核实。
如果同时设了 `SEARCH_PROVIDER` + `SEARCH_API_KEY`，server 会自动用搜索 API 验证并附上证据；
两者缺一即保持关闭。搜索请求失败时降级为「未验证（搜索请求失败）」，报告不会因此中断。

## 快速开始 / 全链路 runbook

两条路径，二选一。两者都在填好 `.env` 之后进行。

### 路径 A：MCP 客户端（Claude Desktop 等）

把 server 注册进客户端配置（见下节「MCP 客户端挂接」），然后在对话里依次让它调用四个 tool。
适合日常对话式探索。

### 路径 B：命令行脚本（最快验证全链路）

仓库自带 `scripts/run_pipeline.py`，按 `sync → tag → query → report` 顺序跑一遍，照抄即可：

```bash
# 1. 只跑同步 + 查询（不需要 LLM key，先确认 GitHub 侧通）
uv run python scripts/run_pipeline.py --query-only

# 2. 全链路（需要 GITHUB_TOKEN + LLM_API_KEY）
uv run python scripts/run_pipeline.py --out ./out
```

每步的预期输出：

- **sync_stars**：`synced`（本次新增行数）、`total_in_db`（库内总数，应 > 0）、`mode`
  （首次为 `full`）。大账号首次全量可能耗时较久（见已知限制 4）。
- **tag_repos**：`readmes`（取到的 README 数）、`tagged`（打标仓库数）、`categories`
  （聚类出的类目数，落在 20–40）、`backfilled`（归类仓库数）、`llm_calls`（本次 LLM 调用数）。
- **query_stars**（示例按类目聚合）：打印 `total` 和各类目计数，总和应与已打标数自洽。
- **generate_report**：返回 `meta`（各节数量、想法数、`search_verified`），并把 `report.md`
  / `ideas.md` 写到 `--out` 目录。报告五节齐全、想法 ≥ 10 条、组合型想法带 A/B 来源与验证行。

一个对话式查询样例（路径 A 里直接问，或路径 B 外单独调 `query_stars`）：
「我 star 的小众 Agent 框架有哪些」→ 先 `aggregate(group_by="category")` 发现类目名，再
`list(category=["Agent 框架"], niche=["极小众","小众"])`。

## MCP 客户端挂接

把 `/abs/path` 换成本仓库的绝对路径：

```json
{
  "mcpServers": {
    "github-star": {
      "command": "uv",
      "args": ["run", "--directory", "/abs/path", "github-star"],
      "env": {
        "GITHUB_TOKEN": "ghp_xxx",
        "LLM_API_KEY": "sk-xxx",
        "LLM_BASE_URL": "https://api.deepseek.com",
        "LLM_MODEL": "deepseek-chat"
      }
    }
  }
}
```

搜索验证可选，再往 `env` 加 `SEARCH_PROVIDER` + `SEARCH_API_KEY`。直接命令行启动 server：

```bash
uv run github-star    # stdio 传输
```

## 四个 tool 详解

### `sync_stars`

```
sync_stars(full: bool = False) -> dict
```

把当前账号的 star 拉进本地 SQLite，按 GitHub repo id 去重。

- **增量（默认）**：结果 star 时间倒序，库里保留最新前缀；一旦整页都已见过就停——star 了一个
  新仓库后重跑只抓前一两页。
- **首次 / 续跑**：空库或上次被中断的库会走完每一页；每页落库即提交，`INSERT OR IGNORE`
  保证被 kill 后续跑无重复。
- **`full=True`**：强制走完全量，用于手动完整校准。

返回 `synced`（新增行）、`total_in_db`、`mode`、起止时间。限流被自动 honor，不静默丢数据。

### `tag_repos`

```
tag_repos(retag_stale: bool = False, batch_size: int = 20, limit: int | None = None) -> dict
```

四阶段打标，每阶段可断点续跑：取 README → round-1 自由打标（3–5 标签 + 受众 + 摘要）→
纯 LLM 聚类成 20–40 个类目 → 回填每仓库一个类目 + 脚本算的小众桶 / 状态。

- 只有未打标（或 `retag_stale=True` 下过期）的仓库才会调 LLM；无新增时重跑 `llm_calls=0`
  （也存于 `meta.last_tag_llm_calls`，可断言）。
- 某仓库 LLM 调用重试仍失败则跳过、留未打标（下次重试），整批不中断、不写半行。
- `batch_size` 控制提交粒度；`limit` 限制处理数量（测试用）。

### `query_stars`

```
query_stars(mode="list", group_by=None, category=[], audience=[], niche=[],
            status=[], language=[], starred_after=None, starred_before=None,
            min_stars=None, max_stars=None, sort="stars_desc",
            limit=50, offset=0) -> dict
```

无需任何外部调用，按五维度查询：功能用途（`category`）、面向用户（`audience`：开发者 / 设计师 /
终端用户 / 运维 / 研究者 / 未知）、小众偏门（`niche`：极小众 / 小众 / 成熟 / 主流）、项目状态
（`status`：活跃 / 停更 / archived）、star 时间（`starred_after/before`，`YYYY` 或
`YYYY-MM-DD`）。各过滤 AND 组合，列表值内部 OR。

- `mode="list"`：分页仓库列表（`limit`/`offset`，`limit` 上限 500）。
- `mode="aggregate"`：需 `group_by`（category/audience/niche/status/year/language），返回每桶计数。
  类目名由 LLM 生成，先 `aggregate(group_by="category")` 发现它们再按名过滤。
- 非法 `mode`/`group_by`/枚举值返回 `{"error": ...}` 结构化错误，不抛异常；空库返回 total 0。

### `generate_report`

```
generate_report(write_to: str | None = None) -> dict
```

读打标好的库（需先 `sync_stars` + `tag_repos`），一次产出：

- `report_markdown`：五节——总览 / 功能类目全景 / 小众宝藏 Top20 / 遗产项目 / 兴趣时间线，
  数字全部源自库内。
- `ideas_markdown`：≥10 条想法（衍生型 + 组合型）；每个仓库引用都经校验确在库内，库外引用整条丢弃。
- `meta`：各节数量、想法数、快照时间。

组合型想法带需求验证字段：配了搜索后端则附 server 端证据，否则带「未验证」交客户端对话核实。
需 `LLM_API_KEY`。`write_to` 为目录时，另把 `report.md` / `ideas.md` 写进去。

## 定期重跑策略

star 会增长、老仓库会停更，建议定期重跑让库保持新鲜。

- **增量 vs 全量 sync**：日常 `sync_stars()` 增量即可（命中已见页就停，很快）；疑似漏数据或想
  完整校准时 `sync_stars(full=True)` 强制全量重走。
- **老仓库过期重打**：`tag_repos(retag_stale=True)` 会把 `analyzed_at` 超过 `TAG_TTL_DAYS`
  （默认 90 天）的仓库删掉旧 round-1 标签后重打。**建议每季度跑一次** `retag_stale=True`。
- **零重复调用保证**：没有新 repo 时普通重跑 `tag_repos()` 的 `llm_calls=0`
  （存于 `meta.last_tag_llm_calls`，可断言），不会浪费 token。
- **重跑副作用**：首次聚类建立 `categories` 后，增量 / 过期重打**不会重建类目表**——避免已回填
  的仓库指向被删的类目名；新仓库按**现有类目**回填。类目体系的整体重建超出当前范围（见已知限制）。
  若你清空 `categories` 触发重聚类，类目名可能随之微变，下游报告 / 查询据此变化属正常。

## 已知限制

如实列出，不掩饰：

1. **改名 / 转移的仓库**：按 GitHub repo id 去重（跨改名稳定），但存储的 `full_name` 不刷新——
   转移后旧名仍在库里。
2. **已有仓库的 metadata 不刷新**：`sync_stars` 只增量加新 star，不更新旧行的 star 数 /
   描述 / 状态等。当前实现用 `INSERT OR IGNORE`，`full=True` 重走也**不覆盖**已存在行——metadata
   刷新整体是未实现项，列入此处而非动手做。
3. **空描述 / 无 topics 的仓库**：打标质量依赖 LLM 对 README 的兜底；无 README 的仓库存空字符串、
   不重复抓取；信息实在太少时 round-1 标签会退化为 `uncategorized`。
4. **API 限流**：`sync_stars` 配额耗尽时会 sleep 到 ratelimit reset（大账号首次全量可能阻塞较久）；
   401（token 无效）不重试，直接失败。
5. **单账号、仅 REST**：一个 `GITHUB_TOKEN`，无 GraphQL、不支持多账号聚合。
6. **LLM 打标 / 聚类非确定性**：类目数硬界 15–50、软目标 20–40，重跑结果可能略有差异；回填偶尔会把
   仓库归到次优类目。
7. **类目体系不随新仓库自动重建**：首次聚类后增量 / 过期重打只按现有类目回填，不重跑聚类——避免悬空
   类目。积累足够多新仓库后的整体重聚类超出当前范围。
8. **组合型想法需求未自动验证**：未配搜索 API 时标注「未验证」，交客户端对话核实；配了但搜索请求
   失败则降级为「未验证（搜索请求失败）」。
9. **真实数据全链路验收待 key**：本周期交付为「可跑通代码 + runbook + mocked 全绿」；真实 key
   到位后再亲跑一次四步全链路补验收证据（顺延至验收时执行）。

## 开发

```bash
uv run pytest
```

测试全程 mock GitHub 与 LLM，不碰真实 API。
