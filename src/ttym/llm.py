"""One OpenAI-compatible chat client for Groq and Gemini."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

import httpx

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY", "openai/gpt-oss-120b"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "GEMINI_API_KEY", "gemini-3.5-flash"),
}


class LLMError(RuntimeError):
    pass


def load_env(path) -> None:
    """Read KEY=value lines into the environment without overriding what is already set."""
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


@dataclass
class LLM:
    provider: str = "groq"
    model: str | None = None
    timeout: float = 60
    usage: dict = field(default_factory=lambda: {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0})

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise LLMError(f"unknown provider {self.provider!r}; use one of {sorted(PROVIDERS)}")
        self.url, key_var, default_model = PROVIDERS[self.provider]
        self.model = self.model or default_model
        self._key = os.environ.get(key_var)
        if not self._key:
            raise LLMError(f"{key_var} is not set")

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, tool_choice: str | None = None) -> dict:
        body: dict = {"model": self.model, "messages": messages, "temperature": 0}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            for attempt in range(4):
                response = await http.post(self.url, json=body, headers={"Authorization": f"Bearer {self._key}"})
                if response.status_code == 429 and attempt < 3:  # per-minute limits: wait as told, then retry
                    await asyncio.sleep(min(float(response.headers.get("retry-after", 10)), 60))
                    continue
                break
        if response.status_code != 200:
            raise LLMError(f"{self.provider} returned {response.status_code}: {response.text[:300]}")
        data = response.json()
        usage = data.get("usage") or {}
        self.usage["requests"] += 1
        self.usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        self.usage["completion_tokens"] += usage.get("completion_tokens", 0)
        return data["choices"][0]["message"]
