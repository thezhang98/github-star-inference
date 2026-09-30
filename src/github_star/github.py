"""GitHub REST client: starred repos with pagination, rate-limit, and retry."""
import random
import time
from typing import Iterator

import httpx

API_ROOT = "https://api.github.com"
STARRED_PATH = "/user/starred"
STAR_ACCEPT = "application/vnd.github.star+json"  # yields {starred_at, repo}
API_VERSION = "2022-11-28"
PER_PAGE = 100

MAX_RETRIES = 5
BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0


class GitHubError(RuntimeError):
    """Non-retryable GitHub API failure (e.g. bad token)."""


class GitHubClient:
    def __init__(self, token: str, client: httpx.Client | None = None,
                 sleep=time.sleep):
        self._token = token
        self._sleep = sleep
        self._client = client or httpx.Client(timeout=30.0)

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": STAR_ACCEPT,
            "X-GitHub-Api-Version": API_VERSION,
        }

    def _honor_rate_limit(self, resp: httpx.Response) -> None:
        """After a successful response, sleep if the quota is exhausted."""
        if resp.headers.get("x-ratelimit-remaining") == "0":
            self._sleep_until_reset(resp)

    def _sleep_until_reset(self, resp: httpx.Response) -> None:
        reset = resp.headers.get("x-ratelimit-reset")
        if reset:
            delay = max(0.0, float(reset) - time.time())
            self._sleep(delay)

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        """Seconds to wait before retrying a 403/429, per GitHub guidance."""
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            return float(retry_after)
        if resp.headers.get("x-ratelimit-remaining") == "0":
            reset = resp.headers.get("x-ratelimit-reset")
            if reset:
                return max(0.0, float(reset) - time.time())
        # secondary rate limit: exponential backoff with jitter
        return min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt)) + random.random()

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        """GET with retry on 403/429 and transient network errors."""
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._client.get(
                    url, headers=self._headers(), params=params,
                )
            except httpx.TransportError as exc:  # timeout / connection error
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise GitHubError(
                        f"network error after {MAX_RETRIES} retries: {exc}"
                    ) from exc
                self._sleep(min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt))
                            + random.random())
                continue

            if resp.status_code == 401:
                raise GitHubError("token 无效或缺权限 (401)")
            if resp.status_code in (403, 429):
                if attempt == MAX_RETRIES:
                    raise GitHubError(
                        f"rate limited ({resp.status_code}) after "
                        f"{MAX_RETRIES} retries"
                    )
                self._sleep(self._retry_delay(resp, attempt))
                continue
            if resp.status_code >= 400:
                raise GitHubError(
                    f"GitHub API error {resp.status_code}: {resp.text[:200]}"
                )
            return resp
        raise GitHubError(f"request failed: {last_exc}")

    def iter_starred(self) -> Iterator[list[dict]]:
        """Yield pages of starred items ({starred_at, repo}), sorted newest first."""
        url: str | None = f"{API_ROOT}{STARRED_PATH}"
        params: dict | None = {
            "per_page": PER_PAGE,
            "page": 1,
            "sort": "created",
            "direction": "desc",
        }
        while url:
            resp = self._get(url, params=params)
            page = resp.json()
            if page:
                yield page
            # advance via Link rel="next"; stop when absent or page empty
            next_url = resp.links.get("next", {}).get("url")
            if not next_url or not page:
                break
            url, params = next_url, None  # next_url already carries query params
            self._honor_rate_limit(resp)
