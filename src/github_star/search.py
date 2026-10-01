"""Optional demand-verification search client (边界四).

Only wired when BOTH SEARCH_PROVIDER and SEARCH_API_KEY are set — otherwise the
report falls back to the client-dialogue channel (未验证 note). The client is a
plain callable ``search(query) -> {"evidence": [url, ...], "conclusion": str}``
so report.py stays provider-agnostic and tests inject a fake instead.
"""
import logging

import httpx

log = logging.getLogger("github_star.search")

_TIMEOUT = 15.0


def build_search(provider: str, api_key: str):
    """Return a ``search(query)`` callable for the given provider, or None."""
    provider = (provider or "").strip().lower()
    if not provider or not api_key:
        return None
    if provider == "tavily":
        return lambda q: _tavily(q, api_key)
    if provider == "brave":
        return lambda q: _brave(q, api_key)
    log.warning("unknown SEARCH_PROVIDER %r; treating as unconfigured", provider)
    return None


def _tavily(query: str, api_key: str) -> dict:
    resp = httpx.post(
        "https://api.tavily.com/search",
        json={"api_key": api_key, "query": query,
              "max_results": 5, "include_answer": True},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    urls = [r["url"] for r in data.get("results", []) if r.get("url")]
    return {"evidence": urls, "conclusion": (data.get("answer") or "").strip()}


def _brave(query: str, api_key: str) -> dict:
    resp = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": 5},
        headers={"X-Subscription-Token": api_key,
                 "Accept": "application/json"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    results = (data.get("web") or {}).get("results", [])
    urls = [r["url"] for r in results if r.get("url")]
    top = results[0].get("description", "").strip() if results else ""
    return {"evidence": urls, "conclusion": top}
