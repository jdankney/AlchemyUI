"""One OpenAI-compatible chat backend, used with schema-constrained output.

Llamacracy's /v1 is a raw passthrough to llama-server, so this one client
talks to it and to the local fallback alike. Failures are classified because
the router treats them differently: an unreachable or slow backend is skipped,
one that cannot see images is skipped only for turns that carry images, and a
malformed reply earns a repair attempt.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from ..db import now


class LLMError(Exception):
    """kind: unreachable | timeout | no_vision | rejected | bad_output"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class LLMReply:
    content: str
    prompt_tokens: int
    completion_tokens: int
    ms: int


class LLMClient:
    def __init__(self, name: str, base_url: str, model: str, api_key: str = "",
                 timeout: float = 45.0, concurrency: int = 2,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.name, self.model = name, model
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # A short connect timeout: a box that is down should fail over in
        # seconds, while a slow answer is allowed the whole budget.
        self._http = httpx.AsyncClient(
            headers=headers, transport=transport,
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)))
        # Planning is capped per backend (spec 7.4): Llamacracy is shared, and
        # the CPU fallback can only usefully do one at a time.
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._ok: bool | None = None
        self._error = ""
        self._at: float | None = None

    def _mark(self, ok: bool, error: str = "") -> None:
        self._ok, self._error, self._at = ok, error, now()

    def status(self) -> dict:
        """For the admin page. `ok` is None until the first call."""
        return {"name": self.name, "model": self.model, "ok": self._ok,
                "error": self._error, "at": self._at}

    async def complete(self, messages: list[dict], schema: dict,
                       max_tokens: int = 400) -> LLMReply:
        """A plan is ~150 tokens. The cap exists for the failure where a model
        loops inside the grammar (router-eval saw ~20s of whitespace-and-
        nothing on a repair), so it should fail fast rather than finish."""
        body = {
            "model": self.model, "messages": messages, "temperature": 0.0,
            # Temperature 0: routing is a classification, and the same message
            # should plan the same way twice (router-eval saw "put me on a
            # beach" flip between likeness and generate at 0.2).
            "max_tokens": max_tokens, "stream": False,
            # Thinking adds latency and nothing to a classification. The
            # template flag is not enough on its own: with an assistant turn in
            # the history (a repair retry) Llamacracy's E4B reasoned anyway,
            # ~250 hidden tokens that overran the cap. The budget stops it.
            "chat_template_kwargs": {"enable_thinking": False},
            "thinking_budget_tokens": 0,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "plan", "strict": True, "schema": schema}},
        }
        t0 = time.monotonic()
        async with self._sem:
            try:
                r = await self._http.post(f"{self.base_url}/chat/completions", json=body)
            except httpx.TimeoutException as e:
                self._mark(False, "timed out")
                raise LLMError("timeout", f"{self.name} timed out") from e
            except httpx.HTTPError as e:
                self._mark(False, f"unreachable ({type(e).__name__})")
                raise LLMError("unreachable", f"{self.name} unreachable ({e})") from e
        ms = round((time.monotonic() - t0) * 1000)

        if r.status_code >= 400:
            text = r.text[:600]
            if "image input is not supported" in text:
                # The box is up; this model just has no vision projector
                # loaded. Not a health failure.
                self._mark(True)
                raise LLMError("no_vision", f"{self.name} ({self.model}) cannot read images")
            kind = "rejected" if r.status_code < 500 else "unreachable"
            self._mark(False, f"HTTP {r.status_code}")
            raise LLMError(kind, f"{self.name}: HTTP {r.status_code}: {text}")
        try:
            d = r.json()
            choice = d["choices"][0]
            content = choice["message"]["content"] or ""
            usage = d.get("usage") or {}
            if choice.get("finish_reason") == "length":
                self._mark(True)
                raise LLMError("bad_output", f"{self.name} ran out of tokens mid-plan")
        except (ValueError, KeyError, IndexError, TypeError) as e:
            self._mark(False, "malformed response")
            raise LLMError("bad_output", f"{self.name}: malformed response") from e
        self._mark(True)
        return LLMReply(content, int(usage.get("prompt_tokens") or 0),
                        int(usage.get("completion_tokens") or 0), ms)

    async def aclose(self) -> None:
        await self._http.aclose()
