import httpx
import pytest

import ttym.llm as llm_module
from ttym.llm import LLM, LLMError, QuotaExhausted

pytestmark = pytest.mark.anyio
OK = {"choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
CACHED = {"choices": [{"message": {"role": "assistant", "content": "hi"}}],
          "usage": {"prompt_tokens": 100, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 80}}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(llm_module.asyncio, "sleep", fake_sleep)
    return slept


def client(*responses):
    queue = list(responses)

    def handler(request):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return LLM(transport=httpx.MockTransport(handler))


async def test_success_counts_usage():
    llm = client(httpx.Response(200, json=OK))
    assert (await llm.chat([{"role": "user", "content": "x"}]))["content"] == "hi"
    assert llm.usage == {"requests": 1, "prompt_tokens": 3, "cached_tokens": 0, "completion_tokens": 1}


async def test_per_minute_limit_waits_then_succeeds(env):
    llm = client(httpx.Response(429, headers={"retry-after": "2"}), httpx.Response(200, json=OK))
    assert (await llm.chat([]))["content"] == "hi"
    assert env == [2.0]


async def test_daily_request_limit_fails_fast(env):
    llm = client(httpx.Response(429, headers={"retry-after": "3", "x-ratelimit-remaining-requests": "0"}))
    with pytest.raises(QuotaExhausted):
        await llm.chat([])
    assert env == []  # no pointless waiting


async def test_long_wait_is_treated_as_exhausted(env):
    llm = client(httpx.Response(429, headers={"retry-after": "600"}))
    with pytest.raises(QuotaExhausted):
        await llm.chat([])
    assert env == []


async def test_wait_budget_is_total_not_per_retry(env):
    llm = client(*[httpx.Response(429, headers={"retry-after": "20"})] * 3)
    with pytest.raises(QuotaExhausted):
        await llm.chat([])
    assert sum(env) <= llm_module.WAIT_BUDGET


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
async def test_network_failures_become_llm_errors(error):
    with pytest.raises(LLMError, match="could not reach"):
        await client(error).chat([])


async def test_server_errors_become_llm_errors():
    with pytest.raises(LLMError, match="500"):
        await client(httpx.Response(500, text="boom")).chat([])


def test_missing_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY")
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        LLM()


async def test_cached_tokens_are_counted_separately():
    llm = client(httpx.Response(200, json=CACHED))
    await llm.chat([])
    assert llm.usage["prompt_tokens"] == 100 and llm.usage["cached_tokens"] == 80


async def test_tool_use_failed_is_its_own_error():
    from ttym.llm import ToolCallRejected

    body = '{"error":{"code":"tool_use_failed","message":"attempted to call tool json"}}'
    with pytest.raises(ToolCallRejected):
        await client(httpx.Response(400, text=body)).chat([])


def test_rejection_detail_keeps_reason_and_generation():
    from ttym.llm import _rejection_detail
    body = '{"error":{"message":"Tool choice is none, but model called a tool","code":"tool_use_failed","failed_generation":"{\\"name\\": \\"get_dimensions\\"}"}}'
    detail = _rejection_detail(body)
    assert "Tool choice is none" in detail and "get_dimensions" in detail
    assert _rejection_detail("not json") == "not json"
