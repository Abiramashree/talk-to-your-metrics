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


async def test_invalid_forced_tool_call_is_retried_without_requirement(server):
    from ttym.llm import ToolCallRejected

    class Rejecting(FakeLLM):
        async def chat(self, messages, tools=None, tool_choice=None):
            if tools:
                self.sent.append({"tools": True, "tool_choice": tool_choice})
                raise ToolCallRejected("invented tool 'json'")
            return await super().chat(messages, tools, tool_choice)

    llm = Rejecting([final({"cannot_answer": "No NPS survey data."})])
    out = await ask("What is our net promoter score?", server, llm)
    assert out["final"] == {"cannot_answer": "No NPS survey data."}
    # first the feedback retry with tools (rejected again), then the retry with none
    assert llm.sent[0] == {"tools": True, "tool_choice": "required"} and llm.sent[1] == {"tools": True, "tool_choice": "auto"}
    assert llm.sent[2]["tools"] is None and len(out["rejected"]) == 2


async def test_tool_call_after_no_tools_retry_ends_the_run(server):
    from ttym.llm import ToolCallRejected

    class AlwaysRejecting(FakeLLM):
        async def chat(self, *a, **k):
            raise ToolCallRejected("Tool choice is none, but model called a tool")

    out = await ask("Revenue?", server, AlwaysRejecting([]))
    assert out["final"] is None and "invalid tool call" in out["error"]


async def test_repeated_call_is_not_rerun(server):
    llm = FakeLLM([
        call("get_dimensions", {"metrics": ["canceled_orders"]}),
        call("get_dimensions", {"metrics": ["canceled_orders"]}, id="c2"),
        final({"cannot_answer": "No category for orders."}),
    ])
    out = await ask("Canceled orders by category?", server, llm)
    first, second = out["trace"]
    assert "not_available" in first["result"]
    assert "already made this exact call" in second["result"]["error"]
    assert out["final"] == {"cannot_answer": "No category for orders."}


async def test_rejected_tool_call_gets_feedback_and_can_retry_the_tool(server):
    from ttym.llm import ToolCallRejected

    class RejectOnce(FakeLLM):
        async def chat(self, messages, tools=None, tool_choice=None):
            if not self.sent:
                self.sent.append({"first": True})
                raise ToolCallRejected("rejected", detail="parameters did not match the schema")
            return await super().chat(messages, tools, tool_choice)

    llm = RejectOnce([
        call("get_dimensions", {"metrics": ["canceled_orders"]}),
        final({"cannot_answer": "No category for orders."}),
    ])
    out = await ask("Cancellation rate for health_beauty?", server, llm)
    retry = llm.sent[1]
    assert retry["tools"] and "parameters did not match the schema" in retry["messages"][-1]["content"]
    assert [s["tool"] for s in out["trace"]] == ["get_dimensions"]
    assert out["final"] == {"cannot_answer": "No category for orders."}
    assert out["rejected"] == ["parameters did not match the schema"]


async def test_final_answer_inside_an_invented_tool_is_used(server):
    import json as _json

    from ttym.llm import ToolCallRejected

    wrapped = _json.dumps({"name": "JSON", "arguments": {"cannot_answer": "No category for orders."}})

    class WrapsFinal(FakeLLM):
        async def chat(self, messages, tools=None, tool_choice=None):
            if self.script:
                return await super().chat(messages, tools, tool_choice)
            raise ToolCallRejected("rejected", detail="attempted to call tool 'JSON'", generation=wrapped)

    llm = WrapsFinal([call("get_dimensions", {"metrics": ["canceled_orders"]})])
    out = await ask("Cancellation rate for health_beauty?", server, llm)
    assert out["final"] == {"cannot_answer": "No category for orders."} and out["rejected"]


def test_a_real_tool_or_bad_answer_is_not_salvaged():
    from ttym.agent import _final_from_rejected
    names = {"list_metrics", "get_dimensions", "query_metrics"}
    assert _final_from_rejected('{"name": "get_dimensions", "arguments": {"cannot_answer": "x"}}', names) is None
    assert _final_from_rejected('{"name": "json", "arguments": {"note": "x"}}', names) is None
    assert _final_from_rejected("not json", names) is None
    assert _final_from_rejected('{"name": "json", "arguments": "{\\"cannot_answer\\": \\"x\\"}"}', names) == {"cannot_answer": "x"}


async def test_follow_up_sees_previous_question_and_query(server):
    llm = FakeLLM([final({"cannot_answer": "x"})])
    previous = {"question": "Top 5 product categories by revenue",
                "query": {"metrics": ["revenue"], "group_by": ["product__category"], "limit": 5}}
    await ask("Which of these had the most canceled orders?", server, llm, previous=previous)
    text = llm.sent[0]["messages"][1]["content"]
    assert '"Top 5 product categories by revenue"' in text and '"product__category"' in text
    assert '"Which of these had the most canceled orders?"' in text


async def test_first_question_is_sent_as_is(server):
    llm = FakeLLM([final({"cannot_answer": "x"})])
    await ask("Revenue in 2017?", server, llm)
    assert llm.sent[0]["messages"][1]["content"] == "Revenue in 2017?"
