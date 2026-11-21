import pytest

from fakes import FakeLLM, call, final
from ttym.agent import MAX_TOOL_CALLS, _inline_refs, ask
from ttym.mcp_server import build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def server(layer):
    return build_server(layer)


async def test_answers_through_mcp_tools(server, answers):
    llm = FakeLLM([
        call("list_metrics", {}),
        call("query_metrics", {"metrics": ["revenue"], "start": "2018-01-01", "end": "2018-03-31"}),
        final({"answer": "x", "columns": ["revenue"], "rows": [[2704438.38]]}),
    ])
    out = await ask("Revenue in Q1 2018?", server, llm)
    assert out["final"]["rows"] == [[2704438.38]]
    assert [s["tool"] for s in out["trace"]] == ["list_metrics", "query_metrics"]
    assert out["trace"][1]["result"]["rows"][0][0] == pytest.approx(answers["d01"]["rows"][0][0])
    assert llm.sent[0]["tool_choice"] == "required" and llm.sent[1]["tool_choice"] == "auto"


async def test_tool_errors_are_fed_back(server):
    llm = FakeLLM([
        call("query_metrics", {"metrics": ["profit"]}),
        final({"cannot_answer": "No profit metric."}),
    ])
    out = await ask("Profit?", server, llm)
    assert out["final"] == {"cannot_answer": "No profit metric."}
    assert "Unknown metric" in out["trace"][0]["result"]["error"]
    assert "Unknown metric" in llm.sent[1]["messages"][-1]["content"]


async def test_one_reminder_then_gives_up(server):
    llm = FakeLLM([call("list_metrics", {}), {"role": "assistant", "content": "It was big."},
                   {"role": "assistant", "content": "Still prose."}])
    out = await ask("Revenue?", server, llm)
    assert out["final"] is None and out["error"] == "no valid final answer"


async def test_tool_call_limit(server):
    script = [call("list_metrics", {}, id=f"c{i}") for i in range(MAX_TOOL_CALLS + 1)]
    llm = FakeLLM(script + [final({"cannot_answer": "gave up"})])
    out = await ask("Loop?", server, llm)
    assert len(out["trace"]) == MAX_TOOL_CALLS + 1
    assert "limit" in out["trace"][-1]["result"]["error"]
    assert llm.sent[-1]["tools"] is None  # no tools offered once the budget is spent


def test_inline_refs_removes_defs():
    schema = {"$defs": {"F": {"type": "object"}}, "properties": {"f": {"items": {"$ref": "#/$defs/F"}}}}
    assert _inline_refs(schema) == {"properties": {"f": {"items": {"type": "object"}}}}


async def test_model_errors_are_not_wrapped(server):
    from ttym.llm import QuotaExhausted

    class Failing:
        usage = {}

        async def chat(self, *a, **k):
            raise QuotaExhausted("used up")

    with pytest.raises(QuotaExhausted):
        await ask("Revenue?", server, Failing())
