"""OpenAI-compatible chat client with JSON-output tolerance and retry.

The client is deliberately thin: it takes an injectable ``complete`` callable
(``messages -> raw_content_str``) so offline tests never construct the real
OpenAI SDK client. ``build_client()`` wires the default one from config.

``chat_json`` returns parsed JSON. Each *logical* LLM operation increments
``call_count`` exactly once (internal retries do not) — that counter is how the
pipeline proves "already-tagged repos trigger zero LLM calls" (§六1).
"""
import json
import random
import time
from typing import Callable

Messages = list[dict]


class LLMError(RuntimeError):
    """Raised when an LLM call fails (all retries exhausted / unparseable)."""


def _parse_json(raw: str) -> dict:
    """Parse a JSON object, tolerating markdown fences and surrounding prose."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # last resort: grab the outermost {...} block
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


class LLMClient:
    def __init__(self, model: str, complete: Callable[[Messages], str],
                 sleep=time.sleep, retries: int = 3):
        self.model = model
        self._complete = complete
        self._sleep = sleep
        self._retries = retries
        self.call_count = 0  # logical LLM operations (assertable in tests)

    def chat_json(self, system: str, user: str) -> dict:
        """One logical LLM call returning parsed JSON.

        Retries up to ``retries`` times (with backoff) on transport errors or
        unparseable output. Raises ``LLMError`` if all attempts fail — the
        caller is expected to skip that repo, never write partial results.
        """
        self.call_count += 1
        messages: Messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        last_exc: Exception | None = None
        for attempt in range(self._retries):
            try:
                return _parse_json(self._complete(messages))
            except Exception as exc:  # noqa: BLE001 — timeout, JSON error, API error
                last_exc = exc
                if attempt < self._retries - 1:
                    self._sleep(min(30.0, 2.0 ** attempt) + random.random())
        raise LLMError(
            f"LLM call failed after {self._retries} attempts: {last_exc}"
        )


def _openai_complete(client, model: str) -> Callable[[Messages], str]:
    def complete(messages: Messages) -> str:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        return resp.choices[0].message.content or ""
    return complete


def build_client(sleep=time.sleep) -> LLMClient:
    """Construct an LLMClient from env config (raises ConfigError if no key)."""
    from openai import OpenAI  # lazy: keep offline tests import-free

    from . import config
    model = config.llm_model()
    client = OpenAI(base_url=config.llm_base_url(), api_key=config.llm_api_key())
    return LLMClient(model, _openai_complete(client, model), sleep=sleep)
