"""End-to-end runbook driver: sync_stars -> tag_repos -> query_stars -> generate_report.

Runs the four MCP tools in order from the command line, so a new user can verify
the whole pipeline without an MCP client. Reads .env from the current directory
(the server itself only reads os.environ; the MCP client passes env via its own
`env` block, and this driver loads .env so the scripted path is one command).

    uv run python scripts/run_pipeline.py            # full run (needs real keys)
    uv run python scripts/run_pipeline.py --query-only  # only sync + query, no LLM

See README.md "快速开始 / 全链路 runbook" for expected output at each step.
"""
import argparse
import json
import os
import sys


def _load_dotenv(path=".env"):
    """Minimal .env loader (no dependency). Real environ wins over .env."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _show(title, result, keys):
    print(f"\n=== {title} ===")
    for k in keys:
        if k in result:
            print(f"  {k}: {json.dumps(result[k], ensure_ascii=False)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--query-only", action="store_true",
                    help="skip tag_repos / generate_report (no LLM key needed)")
    ap.add_argument("--out", default="./out", help="report output dir")
    args = ap.parse_args()

    _load_dotenv()
    # import after .env is loaded so config picks up the values
    from github_star import server

    r = server.sync_stars(full=False)
    _show("sync_stars", r, ["synced", "total_in_db", "mode"])

    if not args.query_only:
        r = server.tag_repos()
        _show("tag_repos", r, ["readmes", "tagged", "categories",
                               "backfilled", "llm_calls"])

    r = server.query_stars(mode="aggregate", group_by="category")
    print(f"\n=== query_stars (aggregate by category) ===")
    print(f"  total: {r.get('total')}")
    for b in r.get("buckets", [])[:10]:
        print(f"    {b['key']}: {b['count']}")

    if not args.query_only:
        r = server.generate_report(write_to=args.out)
        _show("generate_report", r, [])
        print(f"  meta: {json.dumps(r['meta'], ensure_ascii=False)}")
        print(f"  (report.md / ideas.md written to {args.out})")


if __name__ == "__main__":
    sys.exit(main())
