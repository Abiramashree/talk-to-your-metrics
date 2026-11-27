"""The agent: an LLM that answers questions only through our MCP server.

    python -m ttym.agent "What was revenue in the first quarter of 2018?"
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
from typing import Any

from mcp import Client
from mcp.server.mcpserver import MCPServer

from ttym.core import TODAY
from ttym.llm import LLM, LLMError, QuotaExhausted, ToolCallRejected, load_env

MAX_TOOL_CALLS = 8

SYSTEM = f"""\
You answer questions about the Olist marketplace (orders 2016-09 to 2018-10) using the tools. Today is {TODAY}.
Use list_metrics, get_dimensions and query_metrics. Never compute or estimate a number yourself; copy numbers from
query results. If no metric can answer the question, do not substitute a different measure: say it cannot be answered.

When you are done, reply with JSON only, no other text:
{{"answer": "<one short sentence>", "columns": [...], "rows": [[...]]}}  with columns and rows copied from the query result
{{"cannot_answer": "<one short sentence: why>"}}  when the data cannot answer the question"""


# The web chat adds conversation rules. SYSTEM itself stays as benchmarked (bench/run.py uses it).
CHAT_SYSTEM = SYSTEM + """

In this chat, a question may follow an earlier one, shown to you as "Earlier in this conversation".
- A short follow-up such as "by category?", "and 2017?" or "same but delivered" changes the earlier query:
  keep its metrics, filters and dates, and apply only the change.
- If the asked-for split isn't possible for a metric, get_dimensions lists metrics that work with it under
  not_available; use one when it answers the same question (canceled orders by product category:
  orders_by_category with order__status = canceled).
- If the question points back ("these", "that", "them") and there is no earlier question, don't guess:
  reply {"cannot_answer": "Which list do you mean? Ask the first question again, then the follow-up."}"""


def _inline_refs(schema: dict) -> dict:
    """Inline $ref/$defs so the tool schema is self-contained for the model API."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


def _final(content: str) -> dict | None:
    text = re.sub(r"^```(?:json)?|```$", "", (content or "").strip(), flags=re.M).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict) and ("cannot_answer" in value or ("rows" in value and "columns" in value)):
        return value
    return None


def _final_from_rejected(generation: str, tool_names: set[str]) -> dict | None:
    """A final answer the model put inside a call to a tool that doesn't exist, e.g. {"name": "json", "arguments": {...}}."""
    try:
        call = json.loads(generation)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(call, dict) or call.get("name") in tool_names:
        return None
    args = call.get("arguments")
    if isinstance(args, str):
        return _final(args)
    return _final(json.dumps(args)) if isinstance(args, dict) else None


def _user_message(question: str, previous: dict | None) -> str:
    """The question, with the previous question and its query when there is one, so follow-ups like
    "which of these" or "same for 2017" can be understood. The earlier result rows are not sent."""
    if not previous:
        return question
    text = f"Earlier in this conversation the user asked: {json.dumps(previous['question'])}"
    if previous.get("query"):
        text += f"\nIt was answered with this query_metrics call: {json.dumps(previous['query'], sort_keys=True)}"
    return (f"{text}\n\nNow they ask: {json.dumps(question)}\n"
            "If this refers to the earlier question (these, that, same, instead), build on it; otherwise ignore it.")


async def ask(question: str, server: MCPServer, llm: LLM, system: str = SYSTEM, previous: dict | None = None) -> dict:
    """Answer one question. Returns the final answer, every tool call, and token usage.

    previous: {"question": ..., "query": {...} or None}, the last question in the conversation."""
    try:
        return await _ask(_user_message(question, previous), server, llm, system)
    except BaseExceptionGroup as group:
        # The MCP client's task group wraps errors raised inside it; surface the model error itself
        # so callers can handle it (quota, network) instead of seeing an opaque group.
        leaf = _first_leaf(group)
        raise leaf from None


def _first_leaf(group: BaseExceptionGroup) -> BaseException:
    error: BaseException = group
    while isinstance(error, BaseExceptionGroup):
        error = error.exceptions[0]
    return error


async def _ask(question: str, server: MCPServer, llm: LLM, system: str) -> dict:
    trace: list[dict] = []
    rejected: list[str] = []  # tool calls the provider rejected, kept for debugging
    try:
        out = await _loop(question, server, llm, system, trace, rejected)
        if rejected:
            out["rejected"] = rejected
        return out
    except BaseException as error:
        # Keep the tool calls made before the provider failed, so a failed run can be saved with its trace.
        # The MCP client may wrap the error in an exception group; tag the model error inside it.
        leaf = _first_leaf(error) if isinstance(error, BaseExceptionGroup) else error
        if isinstance(leaf, LLMError) and not isinstance(leaf, QuotaExhausted):
            leaf.trace, leaf.usage = trace, dict(llm.usage)
        raise


async def _loop(question: str, server: MCPServer, llm: LLM, system: str, trace: list[dict], rejected: list[str]) -> dict:
    async with Client(server) as client:
        tools = [
            {"type": "function", "function": {"name": t.name, "description": t.description or "",
                                              "parameters": _inline_refs(t.input_schema)}}
            for t in (await client.list_tools()).tools
        ]
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": question}]
        reminded = False
        seen: set[str] = set()  # tool calls already made, so a repeated call can't burn the step budget
        while True:
            calls_left = MAX_TOOL_CALLS - len(trace)
            # gpt-oss tends to answer from its reasoning without calling tools unless the first turn requires one.
            offered = tools if calls_left > 0 else None
            try:
                message = await llm.chat(messages, offered, "required" if not trace else "auto")
            except ToolCallRejected as e:
                salvaged = _final_from_rejected(e.generation, {t["function"]["name"] for t in tools})
                if salvaged is not None:
                    # gpt-oss wrapped its final answer in a call to a tool that doesn't exist ("json"):
                    # the answer itself is valid, so use it.
                    rejected.append(e.detail[:300])
                    return {"final": salvaged, "trace": trace, "usage": dict(llm.usage)}
                # The provider rejected a malformed tool call. First time: say why and let the model try the
                # tool again, since it often still needs one. Otherwise retry with no tools, so it has to
                # reply with the final JSON as text (gpt-oss sometimes wraps that in an invented "json" tool).
                rejected.append(e.detail[:300])
                message = None
                if offered and len(rejected) == 1:
                    feedback = {"role": "user", "content": f"Your last tool call was rejected as invalid: {e.detail[:300]}. "
                                "Call a tool again with arguments that match its schema, or reply with the final JSON."}
                    try:
                        message = await llm.chat(messages + [feedback], offered, "auto")
                    except ToolCallRejected as again:
                        rejected.append(again.detail[:300])
                if message is None:
                    try:
                        message = await llm.chat(messages, None)
                    except ToolCallRejected as last:
                        # Still calling a tool when none is offered: the run ends without an answer (scored wrong).
                        rejected.append(last.detail[:300])
                        return {"final": None, "error": f"invalid tool call after the no-tools retry: {str(last)[:200]}",
                                "trace": trace, "usage": dict(llm.usage)}
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                final = _final(message.get("content", ""))
                if final is not None:
                    return {"final": final, "trace": trace, "usage": dict(llm.usage)}
                if reminded or calls_left <= 0:
                    return {"final": None, "error": "no valid final answer", "content": message.get("content"),
                            "trace": trace, "usage": dict(llm.usage)}
                reminded = True
                messages += [{"role": "assistant", "content": message.get("content") or ""},
                             {"role": "user", "content": "Reply with the final JSON only, in the required format."}]
                continue
            messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": tool_calls})
            for call in tool_calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args, result = {}, {"error": "arguments were not valid JSON"}
                else:
                    key = json.dumps([name, args], sort_keys=True, default=str)
                    if len(trace) >= MAX_TOOL_CALLS:
                        result = {"error": f"tool call limit ({MAX_TOOL_CALLS}) reached; give your final answer"}
                    elif key in seen:
                        result = {"error": "You already made this exact call; its result is above. Use it to answer, "
                                           "or reply with cannot_answer if no metric fits."}
                    else:
                        seen.add(key)
                        r = await client.call_tool(name, args)
                        result = {"error": r.content[0].text} if r.is_error else r.structured_content
                trace.append({"tool": name, "arguments": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, default=str)})


def main() -> None:
    import pathlib

    from ttym.core import SemanticLayer
    from ttym.mcp_server import build_server

    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--bare", action="store_true")
    args = parser.parse_args()
    load_env(pathlib.Path(__file__).resolve().parents[2] / ".env")
    out = asyncio.run(ask(args.question, build_server(SemanticLayer(bare=args.bare)), LLM()))
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
