"""Chat client for Groq's OpenAI-compatible API."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

import httpx

URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "openai/gpt-oss-120b"
WAIT_BUDGET = 45  # seconds we may spend waiting out per-minute limits within one call


class LLMError(RuntimeError):
    """The model could not be reached or refused the request."""


class QuotaExhausted(LLMError):
    """The free limit is used up for now; retrying within this request will not help."""


def load_env(path) -> None:
    """Read KEY=value lines into the environment without overriding what is already set."""
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def _out_for_the_day(response: httpx.Response) -> bool:
    """Documented: x-ratelimit-remaining-requests is the per-day request count. The body text naming a
    per-day limit is an extra, undocumented signal."""
    text = response.text.lower()
    return response.headers.get("x-ratelimit-remaining-requests") == "0" or "per day" in text or "(tpd)" in text


@dataclass
class LLM:
    model: str = MODEL
    timeout: float = 60
    transport: httpx.AsyncBaseTransport | None = None  # tests pass a fake network here
    usage: dict = field(default_factory=lambda: {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})

    def __post_init__(self) -> None:
        self._key = os.environ.get("GROQ_API_KEY")
        if not self._key:
            raise LLMError("GROQ_API_KEY is not set")

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, tool_choice: str | None = None) -> dict:
        body: dict = {"model": self.model, "messages": messages, "temperature": 0}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
        waited = 0.0
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as http:
            while True:
                try:
                    response = await http.post(URL, json=body, headers={"Authorization": f"Bearer {self._key}"})
                except httpx.HTTPError as e:  # connection refused, DNS, timeouts
                    raise LLMError(f"could not reach the model: {type(e).__name__}") from None
                if response.status_code != 429:
                    break
                wait = float(response.headers.get("retry-after", 10))
                # waiting longer than the budget won't help this visitor; neither will waiting out a daily limit
                if _out_for_the_day(response) or waited + wait > WAIT_BUDGET:
                    raise QuotaExhausted("the model's free limit is used up for now")
                await asyncio.sleep(wait)
                waited += wait
        if response.status_code != 200:
            raise LLMError(f"the model returned {response.status_code}")
        data = response.json()
        usage = data.get("usage") or {}
        self.usage["requests"] += 1
        self.usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        self.usage["completion_tokens"] += usage.get("completion_tokens", 0)
        return data["choices"][0]["message"]
